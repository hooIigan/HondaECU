import sys
import os
import wx
import usb1
import pylibftdi
import time
import platform
from threading import Thread
from wx.lib.pubsub import pub
from ecu import *

class USBMonitor(Thread):

	def __init__(self, parent):
		self.parent = parent
		self.usbcontext = usb1.USBContext()
		self.ftdi_devices = {}
		Thread.__init__(self)

	def run(self):
		while self.parent.run:
			time.sleep(.5)
			try:
				new_devices = {}
				for device in self.usbcontext.getDeviceList(skip_on_error=True):
					if device.getVendorID() == pylibftdi.driver.FTDI_VENDOR_ID and device.getProductID() in pylibftdi.driver.USB_PID_LIST:
						serial = device.getSerialNumber()
						new_devices[serial] = device
						if not serial in self.ftdi_devices:
							wx.CallAfter(pub.sendMessage, "USBMonitor", action="add", device=str(device), serial=serial)
				for serial in self.ftdi_devices:
					if not serial in new_devices:
						wx.CallAfter(pub.sendMessage, "USBMonitor", action="remove", device=str(device), serial=serial)
				self.ftdi_devices = new_devices
			except usb1.USBErrorNotSupported:
				if platform.system() == "Windows":
					wx.LogSysError("Incorrect driver for device on %s, install libusbK with Zadig!" % device)
			except usb1.USBErrorPipe:
				pass
			except usb1.USBErrorNoDevice:
				pass
			except usb1.USBErrorIO:
				pass
			except usb1.USBErrorBusy:
				pass

class KlineWorker(Thread):

	def __init__(self, parent):
		self.parent = parent
		self.ecu = None
		self.ready = False
		self.state = 0
		self.tables = None
		pub.subscribe(self.DeviceHandler, "HondaECU.device")
		Thread.__init__(self)

	def DeviceHandler(self, action, device, serial):
		if action == "deactivate":
			if self.ecu:
				wx.LogVerbose("Deactivating device (%s | %s)" % (device, serial))
				self.ecu.dev.close()
				del self.ecu
				self.ecu = None
				self.ready = False
				self.state = 0
				self.tables = None
		elif action == "activate":
			wx.LogVerbose("Activating device (%s | %s)" % (device, serial))
			self.ecu = HondaECU(device_id=serial, dprint=wx.LogDebug)
			self.ecu.setup()
			self.ready = True
			self.state = 0
			self.tables = None

	def run(self):
		while self.parent.run:
			if self.ready and self.ecu:
				try:
					if self.state in [0,12]:
						self.state, status = self.ecu.detect_ecu_state()
						wx.CallAfter(pub.sendMessage, "KlineWorker", info="state", value=(self.state,status))
						wx.LogVerbose("ECU state: %s" % (status))
					elif self.state == 1:
						if self.ecu.ping():
							if not self.tables:
								tables = self.ecu.probe_tables()
								if len(tables) > 0:
									self.tables = tables
									tables = " ".join([hex(x) for x in self.tables])
									wx.LogVerbose("HDS tables: %s" % tables)
						else:
							self.state = 0
				except pylibftdi._base.FtdiError:
					pass
				except AttributeError:
					pass


class HondaECU_GUI(wx.Frame):

	def __init__(self, args, version):
		# Initialize GUI things
		wx.Log.SetActiveTarget(wx.LogStderr())
		wx.Log.SetVerbose(args.verbose)
		if not args.debug:
			wx.Log.SetLogLevel(wx.LOG_Info)
		self.run = True
		self.active_device = None
		self.devices = {}
		title = "HondaECU %s" % (version)
		if getattr(sys, 'frozen', False):
			self.basepath = sys._MEIPASS
		else:
			self.basepath = os.path.dirname(os.path.realpath(__file__))
		ip = os.path.join(self.basepath,"honda.ico")

		# Initialize threads
		self.usbmonitor = USBMonitor(self)
		self.klineworker = KlineWorker(self)

		# Setup GUI
		wx.Frame.__init__(self, None, title=title)
		self.SetMinSize(wx.Size(800,600))
		ib = wx.IconBundle()
		ib.AddIcon(ip)
		self.SetIcons(ib)

		self.statusbar = self.CreateStatusBar(2)
		self.statusbar.SetStatusWidths([140,-1])
		self.statusbar.SetStatusStyles([wx.SB_SUNKEN,wx.SB_SUNKEN])

		self.panel = wx.Panel(self)

		devicebox = wx.StaticBoxSizer(wx.HORIZONTAL, self.panel, "FTDI Devices")
		self.m_devices = wx.Choice(self.panel, wx.ID_ANY)
		devicebox.Add(self.m_devices, 1, wx.EXPAND | wx.ALL, 5)

		mainbox = wx.BoxSizer(wx.VERTICAL)
		mainbox.Add(devicebox, 0, wx.EXPAND | wx.ALL, 10)
		self.panel.SetSizer(mainbox)
		self.panel.Layout()

		# Bind event handlers
		self.Bind(wx.EVT_CLOSE, self.OnClose)
		self.m_devices.Bind(wx.EVT_CHOICE, self.OnDeviceSelected)
		pub.subscribe(self.USBMonitorHandler, "USBMonitor")
		pub.subscribe(self.KlineWorkerHandler, "KlineWorker")

		# Post GUI-setup actions
		self.Centre()
		self.Show()
		self.usbmonitor.start()
		self.klineworker.start()

	def OnClose(self, event):
		self.run = False
		self.usbmonitor.join()
		self.klineworker.join()
		for w in wx.GetTopLevelWindows():
			w.Destroy()

	def OnDeviceSelected(self, event):
		serial = list(self.devices.keys())[self.m_devices.GetSelection()]
		if serial != self.active_device:
			if self.active_device:
				pub.sendMessage("HondaECU.device", action="deactivate", device=self.devices[self.active_device], serial=self.active_device)
			self.active_device = serial
			pub.sendMessage("HondaECU.device", action="activate", device=self.devices[self.active_device], serial=self.active_device)

	def USBMonitorHandler(self, action, device, serial):
		dirty = False
		if action == "add":
			wx.LogVerbose("Adding device (%s | %s)" % (device, serial))
			if not serial in self.devices:
				self.devices[serial] = device
				dirty = True
		elif action =="remove":
			wx.LogVerbose("Removing device (%s | %s)" % (device, serial))
			if serial in self.devices:
				if serial == self.active_device:
					pub.sendMessage("HondaECU.device", action="deactivate", device=self.devices[self.active_device], serial=self.active_device)
					self.active_device = None
					self.statusbar.SetStatusText("", 0)
				del self.devices[serial]
				dirty = True
		if not self.active_device and len(self.devices) > 0:
			self.active_device = list(self.devices.keys())[0]
			pub.sendMessage("HondaECU.device", action="activate", device=self.devices[self.active_device], serial=self.active_device)
			dirty = True
		if dirty:
			self.m_devices.Clear()
			for serial in self.devices:
				self.m_devices.Append(self.devices[serial] + " | " + serial)
		if self.active_device:
			self.m_devices.SetSelection(list(self.devices.keys()).index(serial))

	def KlineWorkerHandler(self, info, value):
		if info == "state":
			self.statusbar.SetStatusText("state: %s" % value[1], 0)
