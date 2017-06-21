#!/usr/bin/env python
# -*- coding: utf-8 -*-

## @package dbus_vrm
# This code takes care of the D-Bus interface (not all of below is implemented yet):
# - on startup it scans the dbus for services we know. For each known service found, it searches for
#   objects/paths we know. Everything we find is stored in items{}, and an event is registered: if a
#   value changes weĺl be notified and can pass that on to our owner. For example the vrmLogger.
#   we know.
# - after startup, it continues to monitor the dbus:
#		1) when services are added we do the same check on that
#		2) when services are removed, we remove any items that we had that referred to that service
#		3) if an existing services adds paths we update ourselves as well: on init, we make a
#          VeDbusItemImport for a non-, or not yet existing objectpaths as well1
#
# Code is used by the vrmLogger, and also the pubsub code. Both are other modules in the dbus_vrm repo.

from dbus.mainloop.glib import DBusGMainLoop
import gobject
from gobject import idle_add
import dbus
import dbus.service
import inspect
import logging
import argparse
import pprint
import traceback
import os
from functools import partial

# our own packages
from vedbus import VeDbusItemExport, VeDbusItemImport
from ve_utils import exit_on_error, wrap_dbus_value, unwrap_dbus_value

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Dictionary containing all devices and paths to look for

items = {}

class DbusMonitor(object):
	## Constructor
	def __init__(self, dbusTree, valueChangedCallback=None, deviceAddedCallback=None,
					deviceRemovedCallback=None, vebusDeviceInstance0=False):
		# valueChangedCallback is the callback that we call when something has changed.
		# def value_changed_on_dbus(dbusServiceName, dbusPath, options, changes, deviceInstance):
		# in which changes is a tuple with GetText() and GetValue()
		self.valueChangedCallback = valueChangedCallback
		self.deviceAddedCallback = deviceAddedCallback
		self.deviceRemovedCallback = deviceRemovedCallback
		self.dbusTree = dbusTree
		self.vebusDeviceInstance0 = vebusDeviceInstance0

		# Lists all tracked services, and their paths per whenToLog option
		self.items = {}

		# Used for fast lookup when handling a dbus PropertyChange signal
		self.serviceIds = {}

		# For a PC, connect to the SessionBus
		# For a CCGX, connect to the SystemBus
		self.dbusConn = dbus.SessionBus() if 'DBUS_SESSION_BUS_ADDRESS' in os.environ else dbus.SystemBus()

		# subscribe to NameOwnerChange for bus connect / disconnect events.
		self.dbusConn.add_signal_receiver(self.dbus_name_owner_changed, signal_name='NameOwnerChanged')

		logger.info('===== Search on dbus for services that we will monitor starting... =====')
		serviceNames = self.dbusConn.list_names()
		for serviceName in serviceNames:
			self.scan_dbus_service(serviceName)
		logger.info('===== Search on dbus for services that we will monitor finished =====')

	def dbus_name_owner_changed(self, name, oldowner, newowner):
		if not name.startswith("com.victronenergy."):
			return

		#decouple, and process in main loop
		idle_add(exit_on_error, self._process_name_owner_changed, name, oldowner, newowner)

	def _process_name_owner_changed(self, name, oldowner, newowner):
		if newowner != '':
			# so we found some new service. Check if we can do something with it.
			newdeviceadded = self.scan_dbus_service(name)
			if newdeviceadded and self.deviceAddedCallback is not None:
				self.deviceAddedCallback(name, self.get_device_instance(name))

		elif name in self.items:
			# it disappeared, we need to remove it.
			logger.info("%s disappeared from the dbus. Removing it from our lists" % name)
			i = self.items[name]['deviceInstance']
			del self.serviceIds[self.items[name]['serviceId']]
			del self.items[name]
			if self.deviceRemovedCallback is not None:
				self.deviceRemovedCallback(name, i)

	def _set_value_callback(self, serviceId, path, options, value):
		self.serviceIds[serviceId]['paths'][path] = [unwrap_dbus_value(value), options]

	def _set_value_error(self, serviceId, path, options, *args):
		self.serviceIds[serviceId]['paths'][path] = [None, options]

	# Scans the given dbus service to see if it contains anything interesting for us. If it does, add
	# it to our list of monitored D-Bus services.
	def scan_dbus_service(self, serviceName):

		# make it a normal string instead of dbus string
		serviceName = str(serviceName)

		paths = self.dbusTree.get('.'.join(serviceName.split('.')[0:3]), None)
		if not paths:
			logger.debug("Ignoring service %s, not in the tree" % serviceName)
			return False

		logger.info("Found: %s, scanning and storing items" % serviceName)

		# we should never be notified to add a D-Bus service that we already have. If this assertion
		# raises, check process_name_owner_changed, and D-Bus workings.
		assert serviceName not in self.items

		service = {}

		# create the empty list items.
		whentologoptions = ['configChange', 'onIntervalAlwaysAndOnEvent', 'onIntervalOnlyWhenChanged',
						'onIntervalAlways']

		# these lists will contain the VeDbusItemImport objects with that whenToLog setting. Used to
		for whentolog in whentologoptions:
			service[whentolog] = []

		serviceId = self.dbusConn.get_name_owner(serviceName)
		service['serviceId'] = serviceId
		self.serviceIds[serviceId] = {'name': serviceName, 'paths': {}}

		service_id_dict = self.serviceIds[serviceId]

		try:
			# for vebus.ttyO1, this is workaround, since VRM Portal expects the main vebus devices at
			# instance 0. Not sure how to fix this yet.
			if serviceName == 'com.victronenergy.vebus.ttyO1' and self.vebusDeviceInstance0:
				device_instance = 0
			else:
				device_instance = VeDbusItemImport(
					self.dbusConn, serviceName, '/DeviceInstance', createsignal=False).get_value()
				device_instance = 0 if device_instance is None else int(device_instance)

			service['deviceInstance'] = device_instance
			logger.info("       %s has device instance %s" % (serviceName, service['deviceInstance']))

			for path, options in paths.iteritems():
				# path will be the D-Bus path: '/Ac/ActiveIn/L1/V'
				# options will be a dictionary: {'code': 'V', 'whenToLog': 'onIntervalAlways'}

				# check that the whenToLog setting is set to something we expect
				assert options['whenToLog'] is None or options['whenToLog'] in whentologoptions

				try:
					self.dbusConn.call_async(serviceName, path, None, 'GetValue', '', [],
							partial(self._set_value_callback, serviceId, path, options),
							partial(self._set_value_error, serviceId, path, options))
				except dbus.exceptions.DBusException as e:
					# TODO: Look into this, perhaps filter more specifically on this error:
					# org.freedesktop.DBus.Error.UnknownMethod
					logger.debug("%s %s does not exist (yet)" % (serviceName, path))
					service_id_dict['paths'][path] = [None, options]

				if options['whenToLog']:
					service[options['whenToLog']].append(path)

				logger.debug("    Added %s%s" % (serviceName, path))

			service['match'] = self.dbusConn.add_signal_receiver(self.handler_value_changes,
				dbus_interface='com.victronenergy.BusItem', signal_name='PropertiesChanged', path_keyword='path',
				sender_keyword='senderId', bus_name=serviceName)

			logger.debug("Finished scanning and storing items for %s" % serviceName)

			# Adjust self at the end of the scan, so we don't have an incomplete set of
			# data if an exception occurs during the scan.
			self.items[serviceName] = service

		except dbus.exceptions.DBusException as e:
			if e.get_dbus_name() == 'org.freedesktop.DBus.Error.ServiceUnknown' or \
				e.get_dbus_name() == 'org.freedesktop.DBus.Error.Disconnected':
				logger.info("Service disappeared while being scanned: %s" % serviceName)
				return False
			else:
				raise

		return True

	def handler_value_changes(self, changes, path, senderId):
		a = self.serviceIds[senderId]['paths'].get(path, None)
		if not a:
			return

		# First update our store to the new value
		a[0] = unwrap_dbus_value(changes['Value'])

		# And do the rest of the processing in on the mainloop
		if self.valueChangedCallback is not None:
			idle_add(exit_on_error, self._execute_value_changes, senderId, path, changes, a[1])

	def _execute_value_changes(self, serviceId, objectPath, changes, options):
		# double check that the service still exists, as it might have
		# disappeared between scheduling-for and executing this function.
		if serviceId not in self.serviceIds:
			return

		serviceName = self.serviceIds[serviceId]['name']
		self.valueChangedCallback(serviceName, objectPath,
			options, changes, self.get_device_instance(serviceName))

	# Gets the value for a certain servicename and path
	# returns the default_value when either the requested service and objectPath combination does not exist,
	# or when its value is INVALID
	def get_value(self, serviceName, objectPath, default_value=None):
		service = self.items.get(serviceName, None)
		if not service:
			return default_value

		value = self.serviceIds[service['serviceId']]['paths'].get(objectPath, None)
		if not value:
			return default_value

		return value[0]

	# Sets the value for a certain servicename and path, returns the return value of the D-Bus SetValue
	# method. If the underlying item does not exist (the service does not exist, or the objectPath was not
	# registered) the function will return -1
	def set_value(self, serviceName, objectPath, value):
		item = self.get_item(serviceName, objectPath)
		if item is None:
			return -1
		return item.set_value(value)

	# returns a dictionary, keys are the servicenames, value the instances
	# optionally use the classfilter to get only a certain type of services, for
	# example com.victronenergy.battery.
	def get_service_list(self, classfilter=None):
		r = {}
		if classfilter is not None:
			class_as_list = classfilter.split('.')[0:3]

		for servicename in self.items.keys():
			if not classfilter or servicename.split('.')[0:3] == class_as_list:
				r[servicename] = self.get_device_instance(servicename)

		return r

	def get_device_instance(self, serviceName):
		return self.items[serviceName]['deviceInstance']

	# Parameter categoryfilter is to be a list, containing the categories you want (configChange,
	# onIntervalAlways, etc).
	# Returns a dictionary, keys are codes + instance, in VRM querystring format. For example vvt[0]. And
	# values are the value.
	def get_values(self, categoryfilter, converter=None):

		result = {}

		for serviceName in self.items.keys():
			result.update(self.get_values_for_service(categoryfilter, serviceName, converter))

		return result

	# same as get_values above, but then for one service only
	def get_values_for_service(self, categoryfilter, servicename, converter=None):
		deviceInstance = self.get_device_instance(servicename)
		result = {}

		service = self.items[servicename]

		for category in categoryfilter:

			for path in service[category]:

				try:
					value, options = self.serviceIds[service['serviceId']]['paths'][path]
				except KeyError:
					value = options = None

				if value is not None:

					value = value if not converter else converter.convert(options['code'], value)

					precision = options.get('precision')
					if precision:
						value = round(value, precision)

					result[options['code'] + "[" + str(deviceInstance) + "]"] = value

		return result


# ====== ALL CODE BELOW THIS LINE IS PURELY FOR DEVELOPING THIS CLASS ======

# Example function that can be used as a starting point to use this code
def value_changed_on_dbus(dbusServiceName, dbusPath, dict, changes, deviceInstance):
	logger.debug("0 ----------------")
	logger.debug("1 %s%s changed" % (dbusServiceName, dbusPath))
	logger.debug("2 vrm dict     : %s" % dict)
	logger.debug("3 changes-text: %s" % changes['Text'])
	logger.debug("4 changes-value: %s" % changes['Value'])
	logger.debug("5 deviceInstance: %s" % deviceInstance)
	logger.debug("6 - end")


def nameownerchange(a, b):
	# used to find memory leaks in dbusmonitor and VeDbusItemImport
	import gc
	gc.collect()
	objects = gc.get_objects()
	print len([o for o in objects if type(o).__name__ == 'VeDbusItemImport'])
	print len([o for o in objects if type(o).__name__ == 'SignalMatch'])
	print len(objects)


# We have a mainloop, but that is just for developing this code. Normally above class & code is used from
# some other class, such as vrmLogger or the pubsub Implementation.
def main():
	# Init logging
	logging.basicConfig(level=logging.DEBUG)
	logger.info(__file__ + " is starting up")

	# Have a mainloop, so we can send/receive asynchronous calls to and from dbus
	DBusGMainLoop(set_as_default=True)

	import os
	import sys
	sys.path.insert(1, os.path.join(os.path.dirname(__file__), '../../'))

	import datalist   # from the dbus_vrm repository
	d = DbusMonitor(datalist.vrmtree, value_changed_on_dbus,
		deviceAddedCallback=nameownerchange, deviceRemovedCallback=nameownerchange)

	logger.info("==configchange values==")
	logger.info(pprint.pformat(d.get_values(['configChange'])))

	logger.info("==onIntervalAlways and onIntervalOnlyWhenChanged==")
	logger.info(pprint.pformat(d.get_values(['onIntervalAlways', 'onIntervalAlwaysAndOnEvent'])))

	# Start and run the mainloop
	logger.info("Starting mainloop, responding on only events")
	mainloop = gobject.MainLoop()
	mainloop.run()

if __name__ == "__main__":
	main()
