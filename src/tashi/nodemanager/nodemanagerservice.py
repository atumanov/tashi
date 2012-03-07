# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import logging
import socket
import threading
import time

from tashi.rpycservices import rpycservices
from tashi.rpycservices.rpyctypes import InstanceState, TashiException, Errors, Instance
from tashi import boolean, vmStates, ConnectionManager
import tashi


class NodeManagerService(object):
	"""RPC handler for the NodeManager

	   Perhaps in the future I can hide the dfs from the
	   VmControlInterface and do all dfs operations here?"""

	def __init__(self, config, vmm):
		self.config = config
		self.vmm = vmm
		self.cmHost = config.get("NodeManagerService", "clusterManagerHost")
		self.cmPort = int(config.get("NodeManagerService", "clusterManagerPort"))
		self.authAndEncrypt = boolean(config.get('Security', 'authAndEncrypt'))
		if self.authAndEncrypt:
			self.username = config.get('AccessClusterManager', 'username')
			self.password = config.get('AccessClusterManager', 'password')
		else:
			self.username = None
			self.password = None
		self.log = logging.getLogger(__file__)
		self.convertExceptions = boolean(config.get('NodeManagerService', 'convertExceptions'))
		self.registerFrequency = float(config.get('NodeManagerService', 'registerFrequency'))
		self.statsInterval = float(self.config.get('NodeManagerService', 'statsInterval'))
		self.registerHost = boolean(config.get('NodeManagerService', 'registerHost'))
		try:
			self.cm = ConnectionManager(self.username, self.password, self.cmPort)[self.cmHost]
		except:
			self.log.exception("Could not connect to CM")
			return

		self.accountingHost = None
		self.accountingPort = None
		try:
			self.accountingHost = self.config.get('NodeManagerService', 'accountingHost')
			self.accountingPort = self.config.getint('NodeManagerService', 'accountingPort')
		except:
			pass

		self.notifyCM = []

		self.__initAccounting()

		self.id = None
		# XXXstroucki this fn could be in this level maybe?
		self.host = self.vmm.getHostInfo(self)

		# populate self.instances
		self.__loadVmInfo()

		self.__registerHost()

		# XXXstroucki: should make an effort to retry
		# otherwise vmm will wait forever
		self.id = self.cm.registerNodeManager(self.host, self.instances.values())

		# XXXstroucki cut cross check for NM/VMM state

		# start service threads
		threading.Thread(target=self.__registerWithClusterManager).start()
		threading.Thread(target=self.__statsThread).start()

	def __initAccounting(self):
		self.accountBuffer = []
		self.accountLines = 0
		self.accountingClient = None
		try:
			if (self.accountingHost is not None) and \
						(self.accountingPort is not None):
				self.accountingClient = ConnectionManager(self.username, self.password, self.accountingPort)[self.accountingHost]
		except:
			self.log.exception("Could not init accounting")

	def __loadVmInfo(self):
		try:
			self.instances = self.vmm.getInstances()
		except Exception:
			self.log.exception('Failed to obtain VM info')
			self.instances = {}

	# send data to CM
	# XXXstroucki adapt this for accounting?
	def __flushNotifyCM(self):
		start = time.time()
		# send data to CM, adding message to buffer if
		# it fails
		try:
			notifyCM = []
			try:
				while (len(self.notifyCM) > 0):
					# XXXstroucki ValueError: need more than 1 value to unpack
					# observed here. How?
					value = self.notifyCM.pop(0)
					(instanceId, newInst, old, success) = value
					try:
						self.cm.vmUpdate(instanceId, newInst, old)
					except TashiException, e:
						notifyCM.append((instanceId, newInst, old, success))
						if (e.errno != Errors.IncorrectVmState):
							raise
					except:
						notifyCM.append((instanceId, newInst, old, success))
						raise
					else:
						success()
			finally:
				if len(notifyCM) > 0:
					self.notifyCM.append(notifyCM)
		except Exception, e:
			self.log.exception('Failed to send data to the CM')

		#toSleep = start - time.time() + self.registerFrequency
		#if (toSleep > 0):
			#time.sleep(toSleep)

	def __ACCOUNTFLUSH(self):
		try:
			if (self.accountingClient is not None):
				self.accountingClient.record(self.accountBuffer)
			self.accountLines = 0
			self.accountBuffer = []
		except:
			self.log.exception("Failed to flush accounting data")


	def __ACCOUNT(self, text, instance=None, host=None):
		now = time.time()
		instanceText = None
		hostText = None

		if instance is not None:
			try:
				instanceText = 'Instance(%s)' % (instance)
			except:
				self.log.exception("Invalid instance data")

		if host is not None:
			try:
				hostText = "Host(%s)" % (host)
			except:
				self.log.exception("Invalid host data")

		secondary = ','.join(filter(None, (hostText, instanceText)))

		line = "%s|%s|%s" % (now, text, secondary)

		self.accountBuffer.append(line)
		self.accountLines += 1

		# XXXstroucki think about force flush every so often
		if (self.accountLines > 0):
			self.__ACCOUNTFLUSH()


	# service thread function
	def __registerWithClusterManager(self):
		while True:
			#self.__ACCOUNT("TESTING")
			start = time.time()
			try:
				instances = self.instances.values()
				self.id = self.cm.registerNodeManager(self.host, instances)
			except Exception:
				self.log.exception('Failed to register with the CM')

			toSleep = start - time.time() + self.registerFrequency
			if (toSleep > 0):
				time.sleep(toSleep)

	# service thread function
	def __statsThread(self):
		if (self.statsInterval == 0):
			return
		while True:
			try:
				publishList = []
				for vmId in self.instances.keys():
					try:
						instance = self.instances.get(vmId, None)
						if (not instance):
							continue
						id = instance.id
						stats = self.vmm.getStats(vmId)
						for stat in stats:
							publishList.append({"vm_%d_%s" % (id, stat):stats[stat]})
					except:
						self.log.exception('statsThread threw an exception')
				if (len(publishList) > 0):
					tashi.publisher.publishList(publishList)
			except:
				self.log.exception('statsThread threw an exception')
			time.sleep(self.statsInterval)

	def __registerHost(self):
		hostname = socket.gethostname()
		# populate some defaults
		# XXXstroucki: I think it's better if the nodemanager fills these in properly when registering with the clustermanager
		memory = 0
		cores = 0
		version = "empty"
		#self.cm.registerHost(hostname, memory, cores, version)

	def __getInstance(self, vmId):
		instance = self.instances.get(vmId, None)
		if instance is not None:
			return instance

		# refresh self.instances if not found
		self.__loadVmInfo()
		instance = self.instances.get(vmId, None)
		if instance is not None:
			return instance


		raise TashiException(d={'errno':Errors.NoSuchVmId,'msg':"There is no vmId %d on this host" % (vmId)})

	# remote
	# Called from VMM to update self.instances
	# but only changes are Exited, MigrateTrans and Running
	# qemu.py calls this in the matchSystemPids thread
	# xenpv.py: i have no real idea why it is called there
	def vmStateChange(self, vmId, old, cur):
		instance = self.__getInstance(vmId)

		if (instance.state == cur):
			# Don't do anything if state is what it should be
			return True

		if (old and instance.state != old):
			# make a note of mismatch, but go on.
			# the VMM should know best
			self.log.warning('VM state was %s, call indicated %s' % (vmStates[instance.state], vmStates[old]))

		instance.state = cur

		self.__ACCOUNT("NM VM STATE CHANGE", instance=instance)

		newInst = Instance(d={'state':cur})
		success = lambda: None
		# send the state change up to the CM
		self.notifyCM.append((instance.id, newInst, old, success))
		self.__flushNotifyCM()

		# cache change locally
		self.instances[vmId] = instance

		if (cur == InstanceState.Exited):
			# At this point, the VMM will clean up,
			# so forget about this instance
			del self.instances[vmId]
			return True

		return True

	# remote
	def createInstance(self, instance):
		vmId = instance.vmId
		self.instances[vmId] = instance


	# remote
	def instantiateVm(self, instance):
		self.__ACCOUNT("NM VM INSTANTIATE", instance=instance)
		try:
			vmId = self.vmm.instantiateVm(instance)
			#instance.vmId = vmId
			#instance.state = InstanceState.Running
			#self.instances[vmId] = instance
			return vmId
		except:
			self.log.exception("Failed to start instance")

	# remote
	def suspendVm(self, vmId, destination):
		instance = self.__getInstance(vmId)
		self.__ACCOUNT("NM VM SUSPEND", instance=instance)

		instance.state = InstanceState.Suspending
		self.instances[vmId] = instance
		threading.Thread(target=self.vmm.suspendVm, args=(vmId, destination)).start()

	# called by resumeVm as thread
	def __resumeVmHelper(self, instance, name):
		self.vmm.resumeVmHelper(instance, name)
		instance.state = InstanceState.Running
		newInstance = Instance(d={'id':instance.id,'state':instance.state})
		success = lambda: None
		self.notifyCM.append((newInstance.id, newInstance, InstanceState.Resuming, success))
		self.__flushNotifyCM()

	# remote
	def resumeVm(self, instance, name):
		self.__ACCOUNT("NM VM RESUME", instance=instance)
		instance.state = InstanceState.Resuming
		instance.hostId = self.id
		try:
			instance.vmId = self.vmm.resumeVm(instance, name)
			self.instances[instance.vmId] = instance
			threading.Thread(target=self.__resumeVmHelper, args=(instance, name)).start()
		except:
			self.log.exception('resumeVm failed')
			raise TashiException(d={'errno':Errors.UnableToResume,'msg':"resumeVm failed on the node manager"})
		return instance.vmId

	# remote
	def prepReceiveVm(self, instance, source):
		self.__ACCOUNT("NM VM MIGRATE RECEIVE PREP")
		instance.vmId = -1
		transportCookie = self.vmm.prepReceiveVm(instance, source.name)
		return transportCookie

	# remote
	def prepSourceVm(self, vmId):
		instance = self.__getInstance(vmId)
		self.__ACCOUNT("NM VM MIGRATE SOURCE PREP", instance=instance)
		instance.state = InstanceState.MigratePrep
		self.instances[vmId] = instance

	# called by migrateVm as thread
	# XXXstroucki migrate out?
	def __migrateVmHelper(self, instance, target, transportCookie):
		self.vmm.migrateVm(instance.vmId, target.name, transportCookie)
		del self.instances[instance.vmId]

	# remote
	# XXXstroucki migrate out?
	def migrateVm(self, vmId, target, transportCookie):
		instance = self.__getInstance(vmId)
		self.__ACCOUNT("NM VM MIGRATE", instance=instance)
		instance.state = InstanceState.MigrateTrans
		self.instances[vmId] = instance
		threading.Thread(target=self.__migrateVmHelper, args=(instance, target, transportCookie)).start()
		return

	# called by receiveVm as thread
	# XXXstroucki migrate in?
	def __receiveVmHelper(self, instance, transportCookie):
		vmId = self.vmm.receiveVm(transportCookie)
		instance.state = InstanceState.Running
		instance.hostId = self.id
		instance.vmId = vmId
		self.instances[vmId] = instance
		newInstance = Instance(d={'id':instance.id,'state':instance.state,'vmId':instance.vmId,'hostId':instance.hostId})
		success = lambda: None
		self.notifyCM.append((newInstance.id, newInstance, InstanceState.Running, success))
		self.__flushNotifyCM()

	# remote
	# XXXstroucki migrate in?
	def receiveVm(self, instance, transportCookie):
		instance.state = InstanceState.MigrateTrans
		vmId = instance.vmId
		self.instances[vmId] = instance
		self.__ACCOUNT("NM VM MIGRATE RECEIVE", instance=instance)
		threading.Thread(target=self.__receiveVmHelper, args=(instance, transportCookie)).start()
		return

	# remote
	def pauseVm(self, vmId):
		instance = self.__getInstance(vmId)
		self.__ACCOUNT("NM VM PAUSE", instance=instance)
		instance.state = InstanceState.Pausing
		self.instances[vmId] = instance
		self.vmm.pauseVm(vmId)
		instance.state = InstanceState.Paused
		self.instances[vmId] = instance

	# remote
	def unpauseVm(self, vmId):
		instance = self.__getInstance(vmId)
		self.__ACCOUNT("NM VM UNPAUSE", instance=instance)
		instance.state = InstanceState.Unpausing
		self.instances[vmId] = instance
		self.vmm.unpauseVm(vmId)
		instance.state = InstanceState.Running
		self.instances[vmId] = instance

	# remote
	def shutdownVm(self, vmId):
		instance = self.__getInstance(vmId)
		self.__ACCOUNT("NM VM SHUTDOWN", instance=instance)
		instance.state = InstanceState.ShuttingDown
		self.instances[vmId] = instance
		self.vmm.shutdownVm(vmId)

	# remote
	def destroyVm(self, vmId):
		instance = self.__getInstance(vmId)
		self.__ACCOUNT("NM VM DESTROY", instance=instance)
		instance.state = InstanceState.Destroying
		self.instances[vmId] = instance
		self.vmm.destroyVm(vmId)

	# remote
	def getVmInfo(self, vmId):
		instance = self.__getInstance(vmId)
		return instance

	# remote
	def vmmSpecificCall(self, vmId, arg):
		return self.vmm.vmmSpecificCall(vmId, arg)

	# remote
	def listVms(self):
		return self.instances.keys()

	# remote
	def liveCheck(self):
		return "alive"
