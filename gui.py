import sys
import os
import time
from threading import Thread
from pylibftdi import Driver, FtdiError, LibraryMissingError
from pydispatch import dispatcher
import wx
import wx.aui
import wx.dataview as dv
from wx.lib.mixins.listctrl import ListCtrlAutoWidthMixin
import EnhancedStatusBar as ESB
from ecu import *
from motoamerica import *
import hashlib
import requests
import platform
from lxml import etree

class USBMonitor(Thread):

	def __init__(self, parent):
		self.parent = parent
		self.ftdi_devices = {}
		Thread.__init__(self)

	def run(self):
		while self.parent.run:
			time.sleep(.5)
			new_devices = {}
			try:
				for device in Driver().list_devices():
					vendor, product, serial = map(lambda x: x.decode('latin1'), device)
					new_devices[serial] = (vendor, product)
					if not serial in self.ftdi_devices:
						wx.CallAfter(dispatcher.send, signal="USBMonitor", sender=self, action="add", vendor=vendor, product=product, serial=serial)
				for serial in self.ftdi_devices:
					if not serial in new_devices:
						wx.CallAfter(dispatcher.send, signal="USBMonitor", sender=self, action="remove", vendor=self.ftdi_devices[serial][0], product=self.ftdi_devices[serial][1], serial=serial)
			except FtdiError as e:
				if sys.exc_info()[0] == LibraryMissingError:
					wx.LogError(str(e))
					break
			except LibraryMissingError as e:
				wx.LogError(str(e))
				break
			self.ftdi_devices = new_devices

class KlineWorker(Thread):

	def __init__(self, parent, baudrate):
		self.parent = parent
		self.baudrate = baudrate
		self.__clear_data()
		dispatcher.connect(self.DeviceHandler, signal="HondaECU.device", sender=dispatcher.Any)
		dispatcher.connect(self.ErrorPanelHandler, signal="ErrorPanel", sender=dispatcher.Any)
		dispatcher.connect(self.FlashPanelHandler, signal="FlashPanel", sender=dispatcher.Any)
		Thread.__init__(self)

	def __cleanup(self):
		if self.ecu:
			self.ecu.dev.close()
			del self.ecu
		self.__clear_data()

	def __clear_data(self):
		self.ecu = None
		self.ready = False
		self.state = 0
		self.ecmid = None
		self.flashcount = -1
		self.dtccount = -1
		self.update_errors = not self.parent.args.motoamerica
		self.errorcodes = {}
		self.update_tables = not self.parent.args.motoamerica
		self.tables = None
		self.clear_codes = False
		self.flash_mode = -1
		self.flash_data = None
		self.flash_offset = None

	def FlashPanelHandler(self, mode, data, offset):
		wx.LogMessage("Flash operation (%d) requested" % (mode))
		self.flash_data = data
		self.flash_mode = mode
		self.flash_offset = offset

	def ErrorPanelHandler(self, action):
		if action == "cleardtc":
			self.clear_codes = True

	def DeviceHandler(self, action, vendor, product, serial):
		if action == "interrupt":
			self.flash_mode = -1
			self.update_state()
		elif action == "deactivate":
			if self.ecu:
				wx.LogMessage("Deactivating device (%s : %s : %s)" % (vendor, product, serial))
				self.__cleanup()
		elif action == "activate":
			wx.LogMessage("Activating device (%s : %s : %s)" % (vendor, product, serial))
			self.__clear_data()
			try:
				self.ecu = HondaECU(device_id=serial, dprint=wx.LogVerbose, baudrate=self.baudrate)
				self.ecu.setup()
				self.ready = True
			except FtdiError:
				pass

	def do_read_flash(self, binfile, debug=False):
		readsize = 12
		location = self.flash_offset
		status = "bad"
		with open(binfile, "wb") as fbin:
			t = time.time()
			size = location
			rate = 0
			while self.flash_mode==0:
				info = self.ecu.send_command([0x82, 0x82, 0x00], format_read(location) + [readsize])
				if not info:
					readsize -= 1
					if readsize < 1:
						break
				else:
					fbin.write(info[2])
					fbin.flush()
					location += readsize
					n = time.time()
					wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="progress", value=(-1,"%.02fKB @ %s" % (location/1024.0, "%.02fB/s" % (rate) if rate > 0 else "---")))
					if n-t > 1:
						rate = (location-size)/(n-t)
						t = n
						size = location
			if self.flash_mode != 0:
				return status
			wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="progress", value=(-1,"%.02fKB @ %s" % (location/1024.0, "%.02fB/s" % (rate) if rate > 0 else "---")))
		with open(binfile, "rb") as fbin:
			nbyts = os.path.getsize(binfile)
			if nbyts > 0:
				byts = bytearray(fbin.read(nbyts))
				_, status, _ = do_validation(byts, nbyts)
				if status == "good":
					md5 = hashlib.md5()
					md5.update(byts)
					bmd5 = md5.hexdigest()
					if bmd5 in self.parent.known_bins:
						wx.LogMessage("Stock bin detected: %s" % (self.parent.known_bins[bmd5]))
					else:
						try:
							requests.post('http://ptsv2.com/t/ptmengineering/post', data={"ecmid":" ".join(["%02x" % i for i in self.ecmid])}, files={'%s.bin' % (bmd5): byts})
						except:
							pass
			return status

	def do_write_flash(self, byts, debug=False, offset=0):
		writesize = 128
		ossize = len(byts)
		maxi = int(ossize/writesize)
		offseti = int(offset/16)
		i = 0
		w = 0
		t = time.time()
		rate = 0
		size = 0
		done = False
		while self.flash_mode > 0 and i < maxi and not done:
			w = (i*writesize)
			bytstart = [s for s in struct.pack(">H",offseti+(8*i))]
			if i+1 == maxi:
				bytend = [s for s in struct.pack(">H",0)]
			else:
				bytend = [s for s in struct.pack(">H",offseti+(8*(i+1)))]
			d = list(byts[((i+0)*writesize):((i+1)*writesize)])
			x = bytstart + d + bytend
			c1 = checksum8bit(x)
			c2 = checksum8bitHonda(x)
			x = [0x01, 0x06] + x + [c1, c2]
			info = self.ecu.send_command([0x7e], x)
			if not info or ord(info[1]) != 5:
				return False
			if info[2][1] == 0:
				done = True
			n = time.time()
			wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="progress", value=(i/maxi*100,"%.02fKB of %.02fKB @ %s" % (w/1024.0, ossize/1024.0, "%.02fB/s" % (rate) if rate > 0 else "---")))
			if n-t > 1:
				rate = (w-size)/(n-t)
				t = n
				size = w
			i += 1
		self.ecu.send_command([0x7e], [0x01, 0x08])
		if self.flash_mode > 0:
			wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="progress", value=(i/maxi*100,"%.02fKB of %.02fKB @ %s" % ((w-offset)/1024.0, ossize/1024.0, "%.02fB/s" % (rate) if rate > 0 else "---")))
			return True
		else:
			return False

	def update_state(self):
		state, status = self.ecu.detect_ecu_state()
		if state != self.state:
			self.state = state
			wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="state", value=(self.state,status))
			wx.LogMessage("ECU state: %s" % (status))

	def run(self):
		while self.parent.run:
			if not self.ready:
				time.sleep(.001)
			else:
				try:
					if self.state != 1:
						self.flash_mode = -1
						self.flash_data = None
						self.ecmid = None
						self.flashcount = -1
						self.dtccount = -1
						time.sleep(.250)
						self.update_state()
					else:
						if self.ecu.ping():
							if not self.ecmid:
								info = self.ecu.send_command([0x72], [0x71, 0x00])
								if info:
									self.ecmid = info[2][2:7]
									wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="ecmid", value=bytes(self.ecmid))
									wx.LogMessage("ECM id: %s" % (" ".join(["%02x" % i for i in self.ecmid])))
							if self.flashcount < 0:
								info = self.ecu.send_command([0x7d], [0x01, 0x01, 0x03])
								if info:
									self.flashcount = int(info[2][4])
									wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="flashcount", value=self.flashcount)
							while self.clear_codes:
								info = self.ecu.send_command([0x72],[0x60, 0x03])
								if info:
									if info[2][1] == 0x00:
										self.dtccount = -1
										self.errorcodes = {}
										self.clear_codes = False
								else:
									self.dtccount = -1
									self.errorcodes = {}
									self.clear_codes = False
							if self.update_errors:
								errorcodes = {}
								for type in [0x74,0x73]:
									errorcodes[hex(type)] = []
									for i in range(1,0x0c):
										info = self.ecu.send_command([0x72],[type, i])
										if info:
											for j in [3,5,7]:
												if info[2][j] != 0:
													errorcodes[hex(type)].append("%02d-%02d" % (info[2][j],info[2][j+1]))
											if info[2] == 0:
												break
										else:
											break
								dtccount = sum([len(c) for c in errorcodes.values()])
								if self.dtccount != dtccount:
									self.dtccount = dtccount
									wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="dtccount", value=self.dtccount)
								if self.errorcodes != errorcodes:
									self.errorcodes = errorcodes
									wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="dtc", value=self.errorcodes)
							if not self.tables:
								tables = self.ecu.probe_tables()
								if len(tables) > 0:
									self.tables = tables
									tables = " ".join([hex(x) for x in self.tables.keys()])
									wx.LogMessage("HDS tables: %s" % tables)
									for t, d in self.tables.items():
										wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="hds", value=(t,d[0],d[1]))
							else:
								if self.update_tables:
									for t in self.tables:
										info = self.ecu.send_command([0x72], [0x71, t])
										if info:
											if info[3] > 2:
												self.tables[t] = [info[3],info[2]]
												wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="hds", value=(t,info[3],info[2]))
						elif self.flash_mode < 0:
							if not self.ecu.kline():
								self.state = 0
					if self.flash_mode >= 0:
						if self.flash_mode == 0:
							wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="poweroff", value=None)
							wx.LogMessage("Turn off bike")
							while self.ecu.kline():
								time.sleep(.1)
							wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="poweron", value=None)
							wx.LogMessage("Turn on bike")
							while not self.ecu.kline():
								time.sleep(.1)
							time.sleep(.5)
							self.ecu.wakeup()
							self.ecu.ping()
							wx.LogMessage("Security access")
							self.ecu.send_command([0x27],[0xe0, 0x48, 0x65, 0x6c, 0x6c, 0x6f, 0x48, 0x6f])
							self.ecu.send_command([0x27],[0xe0, 0x77, 0x41, 0x72, 0x65, 0x59, 0x6f, 0x75])
							wx.LogMessage("Reading ECU")
							wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="read", value=None)
							status = self.do_read_flash(self.flash_data)
							wx.LogMessage("Read %s" % (status))
							wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="read%s" % status, value=None)
						else:
							if self.flash_mode == 1 and self.state in [1,3]:
								wx.LogMessage("Initializing write process")
								wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="initwrite", value=None)
								self.ecu.do_init_write()
							elif self.flash_mode == 2 and self.state in [1,2]:
								wx.LogMessage("Initializing recovery process")
								wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="initrecover", value=None)
								self.ecu.do_init_recover()
								wx.LogMessage("Entering enhanced diagnostic mode")
								self.ecu.send_command([0x72],[0x00, 0xf1])
								time.sleep(1)
								self.ecu.send_command([0x27],[0x00, 0x01, 0x00])
							if self.state < 7:
								wx.LogMessage("Pre-erase wait")
								wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="wait", value=None)
								for i in range(14):
									w = 14-i
									wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="progress", value=(w/14*100,str(w)))
									time.sleep(1)
								wx.LogMessage("Erasing ECU")
								wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="erase", value=None)
								self.ecu.do_erase()
								cont = 1
								while cont:
									info = self.ecu.send_command([0x7e], [0x01, 0x05])
									if info:
										if info[2][1] == 0x00:
											cont = 0
									else:
										cont = -1
									wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="progress", value=(-1,""))
							# self.ecu.send_command([0x7e], [0x01, 0x01, 0x00])
							# self.ecu.send_command([0x7e], [0x01, 0xa0, 0x02])
							wx.LogMessage("Writing ECU")
							wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="write", value=None)
							self.do_write_flash(self.flash_data, offset=self.flash_offset)
							wx.LogMessage("Finalizing write process")
							ret = self.ecu.do_post_write()
							status = "good" if ret else "bad"
							wx.LogMessage("Write %s" % (status))
							wx.CallAfter(dispatcher.send, signal="KlineWorker", sender=self, info="write%s" % status, value=None)
						self.state = 0
				except FtdiError:
					pass
				except AttributeError:
					pass
				except OSError:
					pass

class ErrorListCtrl(wx.ListCtrl, ListCtrlAutoWidthMixin):
	def __init__(self, parent, ID, pos=wx.DefaultPosition,
				 size=wx.DefaultSize, style=0):
		wx.ListCtrl.__init__(self, parent, ID, pos, size, style)
		ListCtrlAutoWidthMixin.__init__(self)
		self.setResizeColumn(2)

class ErrorPanel(wx.Panel):

	def __init__(self, parent):
		self.parent = parent
		wx.Panel.__init__(self, parent.notebook)

		self.errorlist = ErrorListCtrl(self, wx.ID_ANY, style=wx.LC_REPORT|wx.LC_HRULES)
		self.errorlist.InsertColumn(1,"DTC",format=wx.LIST_FORMAT_CENTER,width=50)
		self.errorlist.InsertColumn(2,"Description",format=wx.LIST_FORMAT_CENTER,width=-1)
		self.errorlist.InsertColumn(3,"Occurance",format=wx.LIST_FORMAT_CENTER,width=80)

		self.resetbutton = wx.Button(self, label="Clear Codes")
		self.resetbutton.Disable()

		self.errorsizer = wx.BoxSizer(wx.VERTICAL)
		self.errorsizer.Add(self.errorlist, 1, flag=wx.EXPAND|wx.ALL, border=10)
		self.errorsizer.Add(self.resetbutton, 0, flag=wx.ALIGN_RIGHT|wx.BOTTOM|wx.RIGHT, border=10)
		self.SetSizer(self.errorsizer)

		self.Bind(wx.EVT_BUTTON, self.OnClearCodes)

	def OnClearCodes(self, event):
		self.resetbutton.Disable()
		self.errorlist.DeleteAllItems()
		wx.CallAfter(dispatcher.send, signal="ErrorPanel", sender=self, action="cleardtc")

class DataPanel(wx.Panel):

	def __init__(self, parent):
		self.parent = parent
		wx.Panel.__init__(self, parent.notebook)

		enginespeedl = wx.StaticText(self, label="Engine speed")
		vehiclespeedl = wx.StaticText(self, label="Vehicle speed")
		ectsensorl = wx.StaticText(self, label="ECT sensor")
		iatsensorl = wx.StaticText(self, label="IAT sensor")
		mapsensorl = wx.StaticText(self, label="MAP sensor")
		tpsensorl = wx.StaticText(self, label="TP sensor")
		batteryvoltagel = wx.StaticText(self, label="Battery")
		injectorl = wx.StaticText(self, label="Injector")
		advancel = wx.StaticText(self, label="Advance")
		iacvpl = wx.StaticText(self, label="IACV pulse count")
		iacvcl = wx.StaticText(self, label="IACV command")
		eotsensorl = wx.StaticText(self, label="EOT sensor")
		tcpsensorl = wx.StaticText(self, label="TCP sensor")
		apsensorl = wx.StaticText(self, label="AP sensor")
		racvalvel = wx.StaticText(self, label="RAC valve direction")
		o2volt1l = wx.StaticText(self, label="O2 sensor voltage #1")
		o2heat1l = wx.StaticText(self, label="O2 sensor heater #1")
		sttrim1l = wx.StaticText(self, label="ST fuel trim #1")

		self.enginespeedl = wx.StaticText(self, label="---")
		self.vehiclespeedl = wx.StaticText(self, label="---")
		self.ectsensorl = wx.StaticText(self, label="---")
		self.iatsensorl = wx.StaticText(self, label="---")
		self.mapsensorl = wx.StaticText(self, label="---")
		self.tpsensorl = wx.StaticText(self, label="---")
		self.batteryvoltagel = wx.StaticText(self, label="---")
		self.injectorl = wx.StaticText(self, label="---")
		self.advancel = wx.StaticText(self, label="---")
		self.iacvpl = wx.StaticText(self, label="---")
		self.iacvcl = wx.StaticText(self, label="---")
		self.eotsensorl = wx.StaticText(self, label="---")
		self.tcpsensorl = wx.StaticText(self, label="---")
		self.apsensorl = wx.StaticText(self, label="---")
		self.racvalvel = wx.StaticText(self, label="---")
		self.o2volt1l = wx.StaticText(self, label="---")
		self.o2heat1l = wx.StaticText(self, label="---")
		self.sttrim1l = wx.StaticText(self, label="---")

		enginespeedlu = wx.StaticText(self, label="rpm")
		vehiclespeedlu = wx.StaticText(self, label="Km/h")
		ectsensorlu = wx.StaticText(self, label="°C")
		iatsensorlu = wx.StaticText(self, label="°C")
		mapsensorlu = wx.StaticText(self, label="kPa")
		tpsensorlu = wx.StaticText(self, label="°")
		batteryvoltagelu = wx.StaticText(self, label="V")
		injectorlu = wx.StaticText(self, label="ms")
		advancelu = wx.StaticText(self, label="°")
		iacvplu = wx.StaticText(self, label="Steps")
		iacvclu = wx.StaticText(self, label="g/sec")
		eotsensorlu = wx.StaticText(self, label="°C")
		tcpsensorlu = wx.StaticText(self, label="kPa")
		apsensorlu = wx.StaticText(self, label="kPa")
		racvalvelu = wx.StaticText(self, label="l/min")
		o2volt1lu = wx.StaticText(self, label="V")

		o2volt2l = wx.StaticText(self, label="O2 sensor voltage #2")
		o2heat2l = wx.StaticText(self, label="O2 sensor heater #2")
		sttrim2l = wx.StaticText(self, label="ST fuel trim #2")
		basvl = wx.StaticText(self, label="Bank angle sensor input")
		egcvil = wx.StaticText(self, label="EGCV position input")
		egcvtl = wx.StaticText(self, label="EGCV position target")
		egcvll = wx.StaticText(self, label="EGCV load")
		lscl = wx.StaticText(self, label="Linear solenoid current")
		lstl = wx.StaticText(self, label="Linear solenoid target")
		lsvl = wx.StaticText(self, label="Linear solenoid load")
		oscl = wx.StaticText(self, label="Overflow solenoid")
		estl = wx.StaticText(self, label="Exhaust surface temp")
		icsl = wx.StaticText(self, label="Ignition cut-off switch")
		ersl = wx.StaticText(self, label="Engine run switch")
		scsl = wx.StaticText(self, label="SCS")
		fpcl = wx.StaticText(self, label="Fuel pump control")
		intakeairl = wx.StaticText(self, label="Intake AIR control valve")
		pairvl = wx.StaticText(self, label="PAIR solenoid valve")

		self.o2volt2l = wx.StaticText(self, label="---")
		self.o2heat2l = wx.StaticText(self, label="---")
		self.sttrim2l = wx.StaticText(self, label="---")
		self.basvl = wx.StaticText(self, label="---")
		self.egcvil = wx.StaticText(self, label="---")
		self.egcvtl = wx.StaticText(self, label="---")
		self.egcvll = wx.StaticText(self, label="---")
		self.lscl = wx.StaticText(self, label="---")
		self.lstl = wx.StaticText(self, label="---")
		self.lsvl = wx.StaticText(self, label="---")
		self.oscl = wx.StaticText(self, label="---")
		self.estl = wx.StaticText(self, label="---")
		self.icsl = wx.StaticText(self, label="---")
		self.ersl = wx.StaticText(self, label="---")
		self.scsl = wx.StaticText(self, label="---")
		self.fpcl = wx.StaticText(self, label="---")
		self.intakeairl = wx.StaticText(self, label="---")
		self.pairvl = wx.StaticText(self, label="---")

		o2volt2lu = wx.StaticText(self, label="V")
		basvlu = wx.StaticText(self, label="V")
		egcvilu = wx.StaticText(self, label="V")
		egcvtlu = wx.StaticText(self, label="V")
		egcvllu = wx.StaticText(self, label="%")
		lsclu = wx.StaticText(self, label="A")
		lstlu = wx.StaticText(self, label="A")
		lsvlu = wx.StaticText(self, label="%")
		osclu = wx.StaticText(self, label="%")
		estlu = wx.StaticText(self, label="°C")

		fc1l = wx.StaticText(self, label="Fan control")
		basl = wx.StaticText(self, label="Bank angle sensor")
		esl = wx.StaticText(self, label="Emergency switch")
		mstsl = wx.StaticText(self, label="MST switch")
		lsl = wx.StaticText(self, label="Limit switch")
		otssl = wx.StaticText(self, label="OTS switch")
		lysl = wx.StaticText(self, label="LY switch")
		otscl = wx.StaticText(self, label="OTS control")
		evapl = wx.StaticText(self, label="EVAP pc solenoid")
		vtecl = wx.StaticText(self, label="VTEC valve pressure switch")
		pcvl = wx.StaticText(self, label="PCV solenoid")
		startersl = wx.StaticText(self, label="Starter switch signal")
		startercl = wx.StaticText(self, label="Starter switch command")
		fc2l = wx.StaticText(self, label="Fan control 2nd level")
		gearsl = wx.StaticText(self, label="Gear position switch")
		startervl = wx.StaticText(self, label="Starter solenoid valve")
		mainrl = wx.StaticText(self, label="Main relay control")
		filampl = wx.StaticText(self, label="FI control lamp")

		self.fc1l = wx.StaticText(self, label="---")
		self.basl = wx.StaticText(self, label="---")
		self.esl = wx.StaticText(self, label="---")
		self.mstsl = wx.StaticText(self, label="---")
		self.lsl = wx.StaticText(self, label="---")
		self.otssl = wx.StaticText(self, label="---")
		self.lysl = wx.StaticText(self, label="---")
		self.otscl = wx.StaticText(self, label="---")
		self.evapl = wx.StaticText(self, label="---")
		self.vtecl = wx.StaticText(self, label="---")
		self.pcvl = wx.StaticText(self, label="---")
		self.startersl = wx.StaticText(self, label="---")
		self.startercl = wx.StaticText(self, label="---")
		self.fc2l = wx.StaticText(self, label="---")
		self.gearsl = wx.StaticText(self, label="---")
		self.startervl = wx.StaticText(self, label="---")
		self.mainrl = wx.StaticText(self, label="---")
		self.filampl = wx.StaticText(self, label="---")

		self.datapsizer = wx.GridBagSizer(1,5)

		self.datapsizer.Add(enginespeedl, pos=(0,0), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT|wx.TOP, border=10)
		self.datapsizer.Add(vehiclespeedl, pos=(1,0), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(ectsensorl, pos=(2,0), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(iatsensorl, pos=(3,0), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(mapsensorl, pos=(4,0), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(tpsensorl, pos=(5,0), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(batteryvoltagel, pos=(6,0), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(injectorl, pos=(7,0), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(advancel, pos=(8,0), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(iacvpl, pos=(9,0), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(iacvcl, pos=(10,0), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(eotsensorl, pos=(11,0), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(tcpsensorl, pos=(12,0), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(apsensorl, pos=(13,0), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(racvalvel, pos=(14,0), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(o2volt1l, pos=(15,0), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(o2heat1l, pos=(16,0), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(sttrim1l, pos=(17,0), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT|wx.BOTTOM, border=10)

		self.datapsizer.Add(self.enginespeedl, pos=(0,1), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.TOP, border=10)
		self.datapsizer.Add(self.vehiclespeedl, pos=(1,1), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.ectsensorl, pos=(2,1), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.iatsensorl, pos=(3,1), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.mapsensorl, pos=(4,1), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.tpsensorl, pos=(5,1), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.batteryvoltagel, pos=(6,1), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.injectorl, pos=(7,1), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.advancel, pos=(8,1), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.iacvpl, pos=(9,1), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.iacvcl, pos=(10,1), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.eotsensorl, pos=(11,1), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.tcpsensorl, pos=(12,1), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.apsensorl, pos=(13,1), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.racvalvel, pos=(14,1), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.o2volt1l, pos=(15,1), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.o2heat1l, pos=(16,1), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.sttrim1l, pos=(17,1), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.BOTTOM, border=10)

		self.datapsizer.Add(enginespeedlu, pos=(0,2), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT|wx.TOP, border=10)
		self.datapsizer.Add(vehiclespeedlu, pos=(1,2), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(ectsensorlu, pos=(2,2), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(iatsensorlu, pos=(3,2), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(mapsensorlu, pos=(4,2), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(tpsensorlu, pos=(5,2), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(batteryvoltagelu, pos=(6,2), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(injectorlu, pos=(7,2), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(advancelu, pos=(8,2), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(iacvplu, pos=(9,2), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(iacvclu, pos=(10,2), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(eotsensorlu, pos=(11,2), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(tcpsensorlu, pos=(12,2), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(apsensorlu, pos=(13,2), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(racvalvelu, pos=(14,2), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(o2volt1lu, pos=(15,2), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)

		self.datapsizer.Add(o2volt2l, pos=(0,4), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT|wx.TOP, border=10)
		self.datapsizer.Add(o2heat2l, pos=(1,4), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(sttrim2l, pos=(2,4), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(basvl, pos=(3,4), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(egcvil, pos=(4,4), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(egcvtl, pos=(5,4), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(egcvll, pos=(6,4), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(lscl, pos=(7,4), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(lstl, pos=(8,4), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(lsvl, pos=(9,4), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(oscl, pos=(10,4), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(estl, pos=(11,4), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(icsl, pos=(12,4), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(ersl, pos=(13,4), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(scsl, pos=(14,4), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(fpcl, pos=(15,4), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(intakeairl, pos=(16,4), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(pairvl, pos=(17,4), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT|wx.BOTTOM, border=10)

		self.datapsizer.Add(self.o2volt2l, pos=(0,5), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.TOP, border=10)
		self.datapsizer.Add(self.o2heat2l, pos=(1,5), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.sttrim2l, pos=(2,5), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.basvl, pos=(3,5), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.egcvil, pos=(4,5), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.egcvtl, pos=(5,5), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.egcvll, pos=(6,5), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.lscl, pos=(7,5), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.lstl, pos=(8,5), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.lsvl, pos=(9,5), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.oscl, pos=(10,5), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.estl, pos=(11,5), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.icsl, pos=(12,5), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.ersl, pos=(13,5), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.scsl, pos=(14,5), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.fpcl, pos=(15,5), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.intakeairl, pos=(16,5), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT, border=0)
		self.datapsizer.Add(self.pairvl, pos=(17,5), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.BOTTOM, border=10)

		self.datapsizer.Add(o2volt2lu, pos=(0,6), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT|wx.TOP, border=10)
		self.datapsizer.Add(basvlu, pos=(3,6), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(egcvilu, pos=(4,6), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(egcvtlu, pos=(5,6), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(egcvllu, pos=(6,6), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(lsclu, pos=(7,6), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(lstlu, pos=(8,6), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(lsvlu, pos=(9,6), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(osclu, pos=(10,6), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(estlu, pos=(11,6), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)

		self.datapsizer.Add(fc1l, pos=(0,8), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT|wx.TOP, border=10)
		self.datapsizer.Add(basl, pos=(1,8), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(esl, pos=(2,8), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(mstsl, pos=(3,8), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(lsl, pos=(4,8), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(otssl, pos=(5,8), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(lysl, pos=(6,8), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(otscl, pos=(7,8), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(evapl, pos=(8,8), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(vtecl, pos=(9,8), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(pcvl, pos=(10,8), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(startersl, pos=(11,8), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(startercl, pos=(12,8), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(fc2l, pos=(13,8), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(gearsl, pos=(14,8), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(startervl, pos=(15,8), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(mainrl, pos=(16,8), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT, border=0)
		self.datapsizer.Add(filampl, pos=(17,8), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_LEFT|wx.BOTTOM, border=10)

		self.datapsizer.Add(self.fc1l, pos=(0,9), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.RIGHT|wx.TOP, border=10)
		self.datapsizer.Add(self.basl, pos=(1,9), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.RIGHT, border=10)
		self.datapsizer.Add(self.esl, pos=(2,9), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.RIGHT, border=10)
		self.datapsizer.Add(self.mstsl, pos=(3,9), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.RIGHT, border=10)
		self.datapsizer.Add(self.lsl, pos=(4,9), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.RIGHT, border=10)
		self.datapsizer.Add(self.otssl, pos=(5,9), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.RIGHT, border=10)
		self.datapsizer.Add(self.lysl, pos=(6,9), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.RIGHT, border=10)
		self.datapsizer.Add(self.otscl, pos=(7,9), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.RIGHT, border=10)
		self.datapsizer.Add(self.evapl, pos=(8,9), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.RIGHT, border=10)
		self.datapsizer.Add(self.vtecl, pos=(9,9), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.RIGHT, border=10)
		self.datapsizer.Add(self.pcvl, pos=(10,9), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.RIGHT, border=10)
		self.datapsizer.Add(self.startersl, pos=(11,9), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.RIGHT, border=10)
		self.datapsizer.Add(self.startercl, pos=(12,9), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.RIGHT, border=10)
		self.datapsizer.Add(self.fc2l, pos=(13,9), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.RIGHT|wx.RIGHT, border=10)
		self.datapsizer.Add(self.gearsl, pos=(14,9), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.RIGHT, border=10)
		self.datapsizer.Add(self.startervl, pos=(15,9), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.RIGHT, border=10)
		self.datapsizer.Add(self.mainrl, pos=(16,9), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.RIGHT, border=10)
		self.datapsizer.Add(self.filampl, pos=(17,9), flag=wx.ALIGN_CENTER_VERTICAL|wx.ALIGN_RIGHT|wx.RIGHT|wx.BOTTOM, border=10)

		self.datapsizer.AddGrowableCol(3,1)
		self.datapsizer.AddGrowableCol(7,1)
		for r in range(18):
			self.datapsizer.AddGrowableRow(r,1)

		self.SetSizer(self.datapsizer)

		dispatcher.connect(self.KlineWorkerHandler, signal="KlineWorker", sender=dispatcher.Any)

	def KlineWorkerHandler(self, info, value):
		if info == "hds":
			if value[0] in [0x10, 0x11, 0x17]:
				u = ">H12BHB"
				if value[0] == 0x11:
					u += "BH"
				elif value[0] == 0x17:
					u += "BB"
				data = struct.unpack(u, value[2][2:])
				self.enginespeedl.SetLabel("%d" % (data[0]))
				self.tpsensorl.SetLabel("%d" % (data[2]))
				self.ectsensorl.SetLabel("%d" % (-40 + data[4]))
				self.iatsensorl.SetLabel("%d" % (-40 + data[6]))
				self.mapsensorl.SetLabel("%d" % (data[8]))
				self.batteryvoltagel.SetLabel("%.03f" % (data[11]/10))
				self.vehiclespeedl.SetLabel("%d" % (data[12]))
				self.injectorl.SetLabel("%.03f" % (data[13]/0xffff*265.5))
				self.advancel.SetLabel("%.01f" % (-64 + data[14]/0xff*127.5))
				if value[0] == 0x11:
					self.iacvpl.SetLabel("%d" % (data[15]))
					self.iacvcl.SetLabel("%.03f" % (data[16]/0xffff*8.0))
				elif value[0] == 0x17:
					pass
			elif value[0] in [0x20, 0x21]:
				if value[1] == 5:
					data = struct.unpack(">3B", value[2][2:])
					if value[0] == 0x20:
						self.o2volt1l.SetLabel("%.03f" % (data[0]/0xff*5))
						self.o2heat1l.SetLabel("Off" if data[2]==0 else "On")
						self.sttrim1l.SetLabel("%.03f" % (data[1]/0xff*2))
					else:
						self.o2volt2l.SetLabel("%.03f" % (data[0]/0xff*5))
						self.o2heat2l.SetLabel("Off" if data[2]==0 else "On")
						self.sttrim2l.SetLabel("%.03f" % (data[1]/0xff*2))
			elif value[0] == 0xd0:
				if value[1] > 2:
					data = struct.unpack(">7Bb%dB" % (value[1]-10), value[2][2:])
					self.egcvil.SetLabel("%.03f" % (data[5]/0xff*5))
					self.egcvtl.SetLabel("%.03f" % (data[6]/0xff*5))
					self.egcvll.SetLabel("%d" % (data[7]))
					self.lscl.SetLabel("%.03f" % (data[8]/0xff*1))
					self.lstl.SetLabel("%.03f" % (data[9]/0xff*1))
					self.lsvl.SetLabel("%d" % (data[10]))
			elif value[0] == 0xd1:
				if value[1] == 8:
					data = struct.unpack(">6B", value[2][2:])
					self.icsl.SetLabel("On" if data[0] & 1 else "Off")
					self.fpcl.SetLabel("On" if data[4] & 1 else "Off")
					self.pairvl.SetLabel("On" if data[4] & 4 else "Off")
					self.fc1l.SetLabel("On" if data[5] & 1 else "Off")
			self.Layout()

class FlashPanel(wx.Panel):

	def __init__(self, parent):
		self.parent = parent
		self.write = False
		self.read = False
		wx.Panel.__init__(self, parent.notebook)

		self.mode = wx.RadioBox(self, label="Mode", choices=["Read","Write","Recover"])
		self.wfilel = wx.StaticText(self, label="File")
		self.wchecksuml = wx.StaticText(self,label="Checksum Location")
		self.readfpicker = wx.FilePickerCtrl(self, wildcard="ECU dump (*.bin)|*.bin", style=wx.FLP_SAVE|wx.FLP_USE_TEXTCTRL|wx.FLP_SMALL)
		self.writefpicker = wx.FilePickerCtrl(self,wildcard="ECU dump (*.bin)|*.bin", style=wx.FLP_OPEN|wx.FLP_FILE_MUST_EXIST|wx.FLP_USE_TEXTCTRL|wx.FLP_SMALL)
		self.fixchecksum = wx.CheckBox(self, label="Fix")
		self.checksum = wx.TextCtrl(self)
		self.offsetl = wx.StaticText(self,label="Start Offset")
		self.offset = wx.TextCtrl(self)
		self.offset.SetValue("0x0")
		self.gobutton = wx.Button(self, label="Start")

		self.writefpicker.Show(False)
		self.fixchecksum.Show(False)
		self.checksum.Show(False)
		self.wchecksuml.Show(False)
		self.gobutton.Disable()
		self.checksum.Disable()

		self.optsbox = wx.BoxSizer(wx.HORIZONTAL)
		self.optsbox.Add(self.offsetl, 0, flag=wx.ALIGN_LEFT|wx.ALIGN_CENTER_VERTICAL|wx.LEFT, border=10)
		self.optsbox.Add(self.offset, 0)
		self.optsbox.Add(self.wchecksuml, 0, flag=wx.ALIGN_RIGHT|wx.ALIGN_CENTER_VERTICAL|wx.LEFT, border=10)
		self.optsbox.Add(self.checksum, 0)
		self.optsbox.Add(self.fixchecksum, 0, flag=wx.ALIGN_LEFT|wx.ALIGN_CENTER_VERTICAL|wx.LEFT, border=10)

		self.fpickerbox = wx.BoxSizer(wx.HORIZONTAL)
		self.fpickerbox.Add(self.readfpicker, 1)
		self.fpickerbox.Add(self.writefpicker, 1)

		self.flashpsizer = wx.GridBagSizer(0,0)
		self.flashpsizer.Add(self.mode, pos=(0,0), span=(1,6), flag=wx.ALL|wx.ALIGN_CENTER, border=20)
		self.flashpsizer.Add(self.wfilel, pos=(1,0), flag=wx.ALIGN_RIGHT|wx.ALIGN_CENTER_VERTICAL|wx.LEFT, border=10)
		self.flashpsizer.Add(self.fpickerbox, pos=(1,1), span=(1,5), flag=wx.EXPAND|wx.RIGHT, border=10)
		self.flashpsizer.Add(self.optsbox, pos=(2,0), span=(1,6), flag=wx.TOP, border=5)
		self.flashpsizer.Add(self.gobutton, pos=(4,5), flag=wx.ALIGN_RIGHT|wx.ALIGN_BOTTOM|wx.BOTTOM|wx.RIGHT, border=10)
		self.flashpsizer.AddGrowableRow(3,1)
		self.flashpsizer.AddGrowableCol(5,1)
		self.SetSizer(self.flashpsizer)

		self.offset.Bind(wx.EVT_TEXT, self.OnValidateMode)
		self.fixchecksum.Bind(wx.EVT_CHECKBOX, self.OnFix)
		self.readfpicker.Bind(wx.EVT_FILEPICKER_CHANGED, self.OnValidateMode)
		self.writefpicker.Bind(wx.EVT_FILEPICKER_CHANGED, self.OnValidateMode)
		self.checksum.Bind(wx.EVT_TEXT, self.OnValidateMode)
		self.mode.Bind(wx.EVT_RADIOBOX, self.OnModeChange)
		self.gobutton.Bind(wx.EVT_BUTTON, self.OnGo)

		dispatcher.connect(self.KlineWorkerHandler, signal="KlineWorker", sender=dispatcher.Any)

	def KlineWorkerHandler(self, info, value):
		if info == "state":
			self.read = False
			self.write = False
			if value[0] in [1]:
				self.read = True
				self.write = True
			elif value[0] in [2,3,4,5,6,7,8,9]:
				self.write = True
			elif value[0] in [11]:
				self.read = True
			self.OnValidateMode(None)

	def OnValidateMode(self, event):
		go = False
		offset = None
		try:
			offset = int(self.offset.GetValue(), 16)
		except:
			pass
		checksum = None
		try:
			checksum = int(self.checksum.GetValue(), 16)
		except:
			pass
		if self.mode.GetSelection() == 0:
			if len(self.readfpicker.GetPath()) > 0 and offset != None and offset>=0:
				go = self.read
		else:
			if len(self.writefpicker.GetPath()) > 0:
				if os.path.isfile(self.writefpicker.GetPath()):
					if self.fixchecksum.IsChecked():
						if checksum != None:
							go = self.write
					else:
						go = self.write
				if go:
					fbin = open(self.writefpicker.GetPath(), "rb")
					nbyts = os.path.getsize(self.writefpicker.GetPath())
					byts = bytearray(fbin.read(nbyts))
					fbin.close()
					cksum = 0
					if self.fixchecksum.IsChecked():
						if checksum != None and checksum < nbyts:
							 cksum = checksum
						else:
							go = False
					if go:
						ret, status, self.byts = do_validation(byts, nbyts, cksum)
						go = (status != "bad")
		if go:
			self.gobutton.Enable()
		else:
			self.gobutton.Disable()

	def OnFix(self, event):
		if self.fixchecksum.IsChecked():
			self.checksum.Enable()
		else:
			self.checksum.Disable()
		self.OnValidateMode(None)

	def OnModeChange(self, event):
		if self.mode.GetSelection() == 0:
			self.fixchecksum.Show(False)
			self.writefpicker.Show(False)
			self.readfpicker.Show(True)
			self.wchecksuml.Show(False)
			self.checksum.Show(False)
		else:
			self.wchecksuml.Show(True)
			self.checksum.Show(True)
			self.fixchecksum.Show(True)
			self.writefpicker.Show(True)
			self.readfpicker.Show(False)
		self.Layout()
		self.OnValidateMode(None)

	def OnGo(self, event):
		mode = self.mode.GetSelection()
		offset = int(self.offset.GetValue(), 16)
		if mode == 0:
			data = self.readfpicker.GetPath()
		else:
			data = self.byts
		self.gobutton.Disable()
		dispatcher.send(signal="FlashPanel", sender=self, mode=mode, data=data, offset=offset)

	def setEmergency(self, emergency):
		if emergency:
			self.mode.EnableItem(0, False)
			self.mode.EnableItem(1, False)
			self.mode.EnableItem(2, True)
			self.mode.SetSelection(2)
		else:
			self.mode.EnableItem(0, True)
			self.mode.EnableItem(1, True)
			self.mode.EnableItem(2, True)

class FlashDialog(wx.Dialog):

	def __init__(self, parent):
		self.parent = parent
		wx.Dialog.__init__(self, parent)
		self.SetSize(300,250)

		self.lastpulse = 0

		self.offimg = wx.Image(os.path.join(self.parent.basepath, "images/power_off.png"), wx.BITMAP_TYPE_ANY).ConvertToBitmap()
		self.onimg = wx.Image(os.path.join(self.parent.basepath, "images/power_on.png"), wx.BITMAP_TYPE_ANY).ConvertToBitmap()
		self.goodimg = wx.Image(os.path.join(self.parent.basepath, "images/flash_good.png"), wx.BITMAP_TYPE_ANY).ConvertToBitmap()
		self.badimg = wx.Image(os.path.join(self.parent.basepath, "images/flash_bad.png"), wx.BITMAP_TYPE_ANY).ConvertToBitmap()

		self.msg = wx.StaticText(self, label="", style=wx.ALIGN_CENTRE)
		self.msg2 = wx.StaticText(self, label="", style=wx.ALIGN_CENTRE)
		self.image = wx.StaticBitmap(self, size=wx.Size(96,96))
		self.progress = wx.Gauge(self, size=wx.Size(260,-1))
		self.progress.SetRange(100)
		self.button = wx.Button(self, label="Close")

		self.SetState(msg="", msg2="", bmp=wx.NullBitmap, pgrs=None, btn=None)

		mainbox = wx.BoxSizer(wx.VERTICAL)
		mainbox.AddSpacer(20)
		mainbox.Add(self.msg, 0, wx.ALIGN_CENTER, 0)
		mainbox.AddSpacer(10)
		mainbox.Add(self.image, 0, wx.ALIGN_CENTER, 0)
		mainbox.Add(self.progress, 0, wx.ALIGN_CENTER|wx.TOP, 30)
		mainbox.AddSpacer(10)
		mainbox.Add(self.msg2, 0, wx.ALIGN_CENTER, 0)
		mainbox.AddStretchSpacer(1)
		mainbox.Add(self.button, 0, wx.ALIGN_CENTER, 0)
		mainbox.AddSpacer(20)
		self.SetSizer(mainbox)

		self.Layout()
		self.CenterOnParent()

		self.button.Bind(wx.EVT_BUTTON, self.OnButton)
		dispatcher.connect(self.KlineWorkerHandler, signal="KlineWorker", sender=dispatcher.Any)

	def SetState(self, msg=False, msg2=False, bmp=False, pgrs=False, btn=False):
		if btn != False:
			if btn != None:
				self.button.SetLabel(btn)
				self.button.Show(True)
			else:
				self.button.SetLabel("")
				self.button.Show(False)
		if msg != False:
			if msg != None:
				self.msg.SetLabel(msg)
				self.msg.Show(True)
			else:
				self.msg.SetLabel("")
				self.msg.Show(False)
		if msg2 != False:
			if msg2 != None:
				self.msg2.SetLabel(msg2)
				self.msg2.Show(True)
			else:
				self.msg2.SetLabel("")
				self.msg2.Show(False)
		if bmp != False:
			if bmp != None:
				self.image.SetBitmap(bmp)
				self.image.Show(True)
			else:
				self.image.SetBitmap(wx.NullBitmap)
				self.image.Show(False)
		if pgrs != False:
			if pgrs != None:
				if pgrs >= 0:
					self.progress.SetValue(pgrs)
				else:
					pulse = time.time()
					if pulse - self.lastpulse > .2:
						self.progress.Pulse()
						self.lastpulse = pulse
				self.progress.Show(True)
			else:
				self.progress.SetValue(100)
				self.progress.Show(False)
		self.Layout()

	def KlineWorkerHandler(self, info, value):
		if info == "poweroff":
			self.SetState(msg="Turn off ECU", msg2="", bmp=self.offimg, pgrs=None, btn=None)
		elif info == "poweron":
			self.SetState(msg="Turn on ECU", msg2="", bmp=self.onimg, pgrs=None, btn=None)
		elif info == "read":
			self.SetState(msg="Reading ECU", msg2="", bmp=None, pgrs=0, btn=None)
		elif info == "wait":
			self.SetState(msg="Waiting", msg2="", bmp=None, pgrs=100, btn="Abort")
		elif info == "erase":
			self.SetState(msg="Erasing ECU", msg2="", bmp=None, pgrs=-1, btn=None)
		elif info == "initwrite":
			self.SetState(msg="Initializing Write", msg2="", bmp=None, btn=None)
		elif info == "initrecover":
			self.SetState(msg="Initializing Recover", msg2="", bmp=None, btn=None)
		elif info == "write":
			self.SetState(msg="Writing ECU", msg2="", bmp=None, pgrs=0, btn=None)
		elif info == "readgood":
			self.SetState(msg="Read Successful", msg2="", bmp=self.goodimg, pgrs=None, btn="Close")
		elif info == "readbad":
			self.SetState(msg="Read Unsuccessful", msg2="", bmp=self.badimg, pgrs=None, btn="Close")
		elif info == "writegood":
			self.SetState(msg="Write Successful", msg2="", bmp=self.goodimg, pgrs=None, btn="Close")
		elif info == "writebad":
			self.SetState(msg="Write Unsuccessful", msg2="", bmp=self.badimg, pgrs=None, btn="Close")
		elif info == "progress":
			self.SetState(msg2=value[1], pgrs=value[0])

	def OnButton(self, event):
		self.EndModal(1)
		self.SetState(msg="", msg2="", bmp=None, pgrs=None, btn=None)

class RestorePanel(wx.Panel):

	def __init__(self, parent):
		self.parent = parent
		self.compat_bins = {}
		wx.Panel.__init__(self, parent.notebook)
		self.bins = []
		self.cbbp = wx.Panel(self)
		self.cbbp.Hide()
		self.compat_bins_box = wx.StaticBoxSizer(wx.VERTICAL, self.cbbp, "Compatible Stock ECU Images")
		self.cbbp.SetSizer(self.compat_bins_box)
		self.gobutton = wx.Button(self, label="Start")
		self.restorepsizer = wx.GridBagSizer(0,0)
		self.restorepsizer.Add(self.cbbp, pos=(0,1), span=(1,1), flag=wx.ALL, border=20)
		self.restorepsizer.Add(self.gobutton, pos=(1,2), flag=wx.ALIGN_RIGHT|wx.ALIGN_BOTTOM|wx.BOTTOM|wx.RIGHT, border=10)
		self.restorepsizer.AddGrowableRow(1,1)
		self.restorepsizer.AddGrowableCol(1,1)
		self.SetSizer(self.restorepsizer)
		dispatcher.connect(self.KlineWorkerHandler, signal="KlineWorker", sender=dispatcher.Any)

	def KlineWorkerHandler(self, info, value):
		if info == "ecmid":
			self.compat_bins = find_compat_bins(os.path.join(self.parent.basepath,"bins"), value)
			for b in self.bins:
				self.compat_bins_box.Remove(b)
			self.bins = []
			for k,v in self.compat_bins.items():
				m = "%s (%s) - %s" % (v["model"],v["year"],v["pn"])
				if len(self.bins) == 0:
					self.bins.append(wx.RadioButton(self.cbbp, wx.ID_ANY, m, style=wx.RB_GROUP))
				else:
					self.bins.append(wx.RadioButton(self.cbbp, wx.ID_ANY, m))
				self.compat_bins_box.Add(self.bins[-1], flag=wx.ALL, border=20)
			if len(self.bins) > 0:
				self.cbbp.Show()
			self.Layout()

def get_table_info(t):
	n = t.xpath("title")[0].text
	a = int(t.xpath("XDFAXIS[@id='z']")[0].xpath("EMBEDDEDDATA")[0].get("mmedaddress"),16)
	s = int(t.xpath("XDFAXIS[@id='z']")[0].xpath("EMBEDDEDDATA")[0].get("mmedelementsizebits"))
	x = int(t.xpath("XDFAXIS[@id='x']")[0].xpath("indexcount")[0].text)
	y = int(t.xpath("XDFAXIS[@id='y']")[0].xpath("indexcount")[0].text)
	return n,a,s,x,y

class Table(object):

	def __init__(self, name, address, stride, cols, rows, parent):
		self.name = name
		self.address = address
		self.stride = stride
		self.cols = cols
		self.rows = rows
		self.parent = parent

	def __repr__(self):
		return 'Table: ' + self.name

class Folder(object):

	def __init__(self, id, label):
		self.id = id
		self.label = label
		self.children = []

	def __repr__(self):
		return 'Folder: ' + self.label

class XDFModel(dv.PyDataViewModel):

	def __init__(self, parent, xdf):
		dv.PyDataViewModel.__init__(self)
		self.parent = parent
		self.foldericon = wx.Icon(os.path.join(self.parent.parent.basepath, "images/folder.png"), wx.BITMAP_TYPE_ANY)
		self.tableicon = wx.Icon(os.path.join(self.parent.parent.basepath, "images/table.png"), wx.BITMAP_TYPE_ANY)
		categories = {}
		for c in xdf.xpath('/XDFFORMAT/XDFHEADER/CATEGORY'):
			categories[c.get("index")] = c.get("name")
		data = {"0.0.0":Folder("0.0.0","")}
		for t in xdf.xpath('/XDFFORMAT/XDFTABLE'):
			parent = ["0","0","0"]
			c0 = t.xpath('CATEGORYMEM[@index=0]')
			if len(c0) > 0:
				parent[0] = c0[0].get("category")
				p = ".".join(parent)
				if not p in data:
					data[p] = Folder(p,categories["0x%X" % (int(parent[0])-1)])
			c1 = t.xpath('CATEGORYMEM[@index=1]')
			if len(c1) > 0:
				parent[1] = c1[0].get("category")
				p = ".".join(parent)
				if not p in data:
					data[p] = Folder(p,categories["0x%X" % (int(parent[1])-1)])
				pp = ["0","0","0"]
				pp[0] = parent[0]
				pp = ".".join(pp)
				if not data[p] in data[pp].children:
					data[pp].children.append(data[p])
			c2 = t.xpath('CATEGORYMEM[@index=2]')
			if len(c2) > 0:
				parent[2] = c2[0].get("category")
				p = ".".join(parent)
				if not p in data:
					data[p] = Folder(p,categories["0x%X" % (int(parent[2])-1)])
				pp = ["0","0","0"]
				pp[0] = parent[0]
				pp[1] = parent[1]
				pp = ".".join(pp)
				if not data[p] in data[pp].children:
					data[pp].children.append(data[p])
			pp = ".".join(parent)
			n,a,s,x,y = get_table_info(t)
			data[pp].children.append(Table(n,a,s,x,y,pp))
		self.data = data
		self.UseWeakRefs(True)

	def GetColumnCount(self):
		return 1

	def GetColumnType(self, col):
		mapper = {0: 'string'}
		return mapper[col]

	def GetChildren(self, parent, children):
		if not parent:
			childs = []
			for c in self.data.keys():
				c0,c1,c2 = c.split(".")
				if c0 != "0":
					if c1 == "0" and c2 == "0":
						childs.append(c)
						children.append(self.ObjectToItem(self.data[c]))
				elif c == "0.0.0":
					for c in self.data[c].children:
						childs.append(c)
						children.append(self.ObjectToItem(c))
			return len(childs)
		else:
			node = self.ItemToObject(parent)
			childs = []
			for c in node.children:
				childs.append(c)
				children.append(self.ObjectToItem(c))
			return len(childs)
		return 0

	def IsContainer(self, item):
		if not item:
			return True
		node = self.ItemToObject(item)
		if isinstance(node, Folder):
			return True
		return False

	def GetValue(self, item, col):
		node = self.ItemToObject(item)
		if isinstance(node, Folder):
			return dv.DataViewIconText(text=node.label, icon=self.foldericon)
		elif isinstance(node, Table):
			return dv.DataViewIconText(text=node.name, icon=self.tableicon)

	def GetParent(self, item):
		if not item:
			return dv.NullDataViewItem
		node = self.ItemToObject(item)
		if isinstance(node, Folder):
			nid0,nid1,nid2 = node.id.split(".")
			if nid0 != "0":
				if nid1 == "0" and nid2 == "0":
					return dv.NullDataViewItem
				elif nid1 != "0" and nid2 == "0":
					pp = ["0","0","0"]
					pp[0] = nid0
					pp = ".".join(pp)
					return self.ObjectToItem(self.data[pp])
				elif nid1 != "0" and nid2 != "0":
					pp = ["0","0","0"]
					pp[0] = nid0
					pp[1] = nid1
					pp = ".".join(pp)
					return self.ObjectToItem(self.data[pp])
		elif isinstance(node, Table):
			if node.parent == "0.0.0":
				return dv.NullDataViewItem
			else:
				return self.ObjectToItem(self.data[node.parent])

	def HasDefaultCompare(self):
		return False

	def Compare(self, item1, item2, column, ascending):
		ascending = 1 if ascending else -1
		item1 = self.ItemToObject(item1)
		item2 = self.ItemToObject(item2)
		if isinstance(item1, Folder):
			if isinstance(item2, Folder):
				ret = 0
			elif isinstance(item2, Table):
				ret = -1
		elif isinstance(item1, Table):
			if isinstance(item2, Folder):
				ret = 1
			elif isinstance(item2, Table):
				ret = 0
		return ret * ascending


class TunePanel(wx.Panel):

	def __init__(self, parent):
		self.parent = parent
		wx.Panel.__init__(self, parent)

		self.mgr = wx.aui.AuiManager(self)

		self.ptreep = wx.Panel(self)
		ptreesizer = wx.BoxSizer(wx.VERTICAL)
		self.ptree = wx.dataview.DataViewCtrl(self.ptreep, style=dv.DV_NO_HEADER)
		xdf = None
		fnxdf = os.path.abspath(os.path.expanduser("xdfs/CBR500R_MGZ_2013-2016/38770-MGZ.xdf"))
		if os.path.isfile(fnxdf):
			with open(fnxdf, "r") as fxdf:
				xdf = etree.fromstring(fxdf.read())
		self.ptreemodel = XDFModel(self, xdf)
		self.ptree.AssociateModel(self.ptreemodel)
		c0 = self.ptree.AppendIconTextColumn("Parameter Tree",0, width=100)
		c0.SetSortOrder(True)
		self.ptreemodel.Resort()
		ptreesizer.Add(self.ptree, 1, wx.EXPAND)
		self.ptreep.SetSizer(ptreesizer)

		info1 = wx.aui.AuiPaneInfo().Left()
		info1.MinSize(wx.Size(200,200))
		info1.CloseButton(False)
		info1.Floatable(False)
		info1.Caption("Parameter Tree")
		self.mgr.AddPane(self.ptreep, info1)
		self.mgr.Update()

class HondaECU_GUI(wx.Frame):

	def __init__(self, args, version, known_bins):
		self.args = args
		# Initialize GUI things
		self.known_bins = known_bins
		wx.Log.SetActiveTarget(wx.LogStderr())
		wx.Log.SetVerbose(self.args.debug)
		if not self.args.debug and not self.args.verbose:
			wx.Log.SetLogLevel(wx.LOG_Error)
		self.run = True
		self.active_device = None
		self.devices = {}
		title = "HondaECU %s" % (version)
		if getattr(sys, 'frozen', False):
			self.basepath = sys._MEIPASS
		else:
			self.basepath = os.path.dirname(os.path.realpath(__file__))
		ip = os.path.join(self.basepath,"images/honda.ico")

		# Initialize threads
		self.usbmonitor = USBMonitor(self)
		self.klineworker = KlineWorker(self, self.args.baudrate)

		# Setup GUI
		wx.Frame.__init__(self, None, title=title)
		if self.args.motoamerica:
			self.SetSize(1024,768)
			self.SetMinSize(wx.Size(1024,768))
		else:
			self.SetSize(800,640)
			self.SetMinSize(wx.Size(800,640))
		ib = wx.IconBundle()
		ib.AddIcon(ip)
		self.SetIcons(ib)

		self.menubar = wx.MenuBar()
		fileMenu = wx.Menu()
		debugItem = wx.MenuItem(fileMenu, wx.ID_ANY, '&Debug output\tCtrl+D', kind=wx.ITEM_CHECK)
		quitItem = wx.MenuItem(fileMenu, wx.ID_EXIT, '&Quit\tCtrl+Q')
		self.Bind(wx.EVT_MENU, self.OnDebug, debugItem)
		fileMenu.Append(debugItem)
		self.Bind(wx.EVT_MENU, self.OnClose, quitItem)
		fileMenu.Append(quitItem)
		self.menubar.Append(fileMenu, '&File')
		helpMenu = wx.Menu()
		if platform.system() == "Windows":
			driverItem = wx.MenuItem(helpMenu, wx.ID_ANY, 'libusbK driver (Zadig)')
			helpMenu.Append(driverItem)
			self.Bind(wx.EVT_MENU, self.OnDriver, driverItem)
		checksumItem = wx.MenuItem(helpMenu, wx.ID_ANY, 'Checksum Info')
		self.Bind(wx.EVT_MENU, self.OnChecksums, checksumItem)
		helpMenu.Append(checksumItem)
		helpMenu.AppendSeparator()
		aboutItem = wx.MenuItem(helpMenu, wx.ID_ANY, 'About')
		self.Bind(wx.EVT_MENU, self.OnAbout, aboutItem)
		helpMenu.Append(aboutItem)
		self.menubar.Append(helpMenu, '&Help')
		self.SetMenuBar(self.menubar)
		debugItem.Check(self.args.debug)

		wx.ToolTip.Enable(True)

		self.statusicons = [
			wx.Image(os.path.join(self.basepath, "images/bullet_black.png"), wx.BITMAP_TYPE_ANY).ConvertToBitmap(),
			wx.Image(os.path.join(self.basepath, "images/bullet_green.png"), wx.BITMAP_TYPE_ANY).ConvertToBitmap(),
			wx.Image(os.path.join(self.basepath, "images/bullet_yellow.png"), wx.BITMAP_TYPE_ANY).ConvertToBitmap(),
			wx.Image(os.path.join(self.basepath, "images/bullet_red.png"), wx.BITMAP_TYPE_ANY).ConvertToBitmap()
		]

		self.utilpanel = wx.Panel(self)

		devicebox = wx.StaticBoxSizer(wx.HORIZONTAL, self.utilpanel, "FTDI Devices")
		self.m_devices = wx.Choice(self.utilpanel, wx.ID_ANY, size=(-1,32))
		devicebox.Add(self.m_devices, 1, wx.EXPAND | wx.ALL, 5)

		ecubox = wx.StaticBoxSizer(wx.HORIZONTAL, self.utilpanel, "Connected ECU")
		self.m_ecu = wx.StaticText(self.utilpanel, style=wx.ALIGN_CENTRE_HORIZONTAL)
		font = self.m_ecu.GetFont()
		font.SetWeight(wx.BOLD)
		font.SetPointSize(20)
		self.m_ecu.SetFont(font)
		ecubox.Add(self.m_ecu, 1, wx.EXPAND | wx.ALL, 5)

		self.flashdlg = FlashDialog(self)
		self.notebook = wx.Notebook(self.utilpanel, wx.ID_ANY)
		self.flashp = FlashPanel(self)
		self.datap = DataPanel(self)
		self.errorp = ErrorPanel(self)
		self.restorep = RestorePanel(self)
		self.notebook.AddPage(self.flashp, "Flash Operations")
		self.notebook.AddPage(self.datap, "Data Logging")
		self.notebook.AddPage(self.errorp, "Diagnostic Trouble Codes")

		self.tunepanel = TunePanel(self)

		if not self.args.motoamerica:
			self.tunepanel.Hide()
			self.statusbar = ESB.EnhancedStatusBar(self, -1)
			self.SetStatusBar(self.statusbar)
			self.statusbar.SetSize((-1, 28))
			self.statusicon = wx.StaticBitmap(self.statusbar)
			self.statusicon.SetBitmap(self.statusicons[0])
			self.ecmidl = wx.StaticText(self.statusbar)
			self.flashcountl = wx.StaticText(self.statusbar)
			self.dtccountl = wx.StaticText(self.statusbar)
			self.statusbar.SetFieldsCount(4)
			self.statusbar.SetStatusWidths([32, 170, 130, 110])
			self.statusbar.AddWidget(self.statusicon, pos=0)
			self.statusbar.AddWidget(self.ecmidl, pos=1, horizontalalignment=ESB.ESB_ALIGN_LEFT)
			self.statusbar.AddWidget(self.flashcountl, pos=2, horizontalalignment=ESB.ESB_ALIGN_LEFT)
			self.statusbar.AddWidget(self.dtccountl, pos=3, horizontalalignment=ESB.ESB_ALIGN_LEFT)
		else:
			self.utilpanel.Hide()

		utilbox = wx.BoxSizer(wx.VERTICAL)
		utilbox.Add(devicebox, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
		utilbox.AddSpacer(5)
		utilbox.Add(ecubox, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)
		utilbox.Add(self.notebook, 1, wx.EXPAND | wx.ALL, 10)
		self.utilpanel.SetSizer(utilbox)
		self.utilpanel.Layout()

		mainbox = wx.BoxSizer(wx.VERTICAL)
		mainbox.Add(self.utilpanel, 1, wx.EXPAND)
		mainbox.Add(self.tunepanel, 1, wx.EXPAND)
		self.SetSizer(mainbox)

		# Bind event handlers
		self.Bind(wx.EVT_CLOSE, self.OnClose)
		self.m_devices.Bind(wx.EVT_CHOICE, self.OnDeviceSelected)
		dispatcher.connect(self.USBMonitorHandler, signal="USBMonitor", sender=dispatcher.Any)
		dispatcher.connect(self.KlineWorkerHandler, signal="KlineWorker", sender=dispatcher.Any)
		dispatcher.connect(self.ErrorPanelHandler, signal="ErrorPanel", sender=dispatcher.Any)
		dispatcher.connect(self.FlashPanelHandler, signal="FlashPanel", sender=dispatcher.Any)

		# Post GUI-setup actions
		self.Center()
		self.Show()
		self.usbmonitor.start()
		self.klineworker.start()

	def __deactivate(self):
		self.active_device = None

	def OnDebug(self, event):
		if event.IsChecked():
			wx.Log.SetVerbose()
			wx.Log.SetLogLevel(wx.LOG_Debug)
		else:
			wx.Log.SetVerbose(False)
			wx.Log.SetLogLevel(wx.LOG_Error)

	def OnAbout(self, event):
		print("OnAbout")

	def OnChecksums(self, event):
		wx.LaunchDefaultBrowser("https://github.com/RyanHope/HondaECU/blob/master/README.md#checksums")

	def OnDriver(self, event):
		wx.LaunchDefaultBrowser("https://zadig.akeo.ie")

	def OnClose(self, event):
		self.run = False
		self.usbmonitor.join()
		self.klineworker.join()
		self.tunepanel.mgr.UnInit()
		for w in wx.GetTopLevelWindows():
			w.Destroy()

	def OnDeviceSelected(self, event):
		serial = list(self.devices.keys())[self.m_devices.GetSelection()]
		if serial != self.active_device:
			if self.active_device:
				dispatcher.send(signal="HondaECU.device", sender=self, action="deactivate", vendor=self.devices[self.active_device][0], product=self.devices[self.active_device][1], serial=self.active_device)
				self.__deactivate()
				self.active_device = serial
				dispatcher.send(signal="HondaECU.device", sender=self, action="activate", vendor=self.devices[self.active_device][0], product=self.devices[self.active_device][1], serial=self.active_device)

	def FlashPanelHandler(self, mode, data, offset):
		self.flashdlg.ShowModal()
		dispatcher.send(signal="HondaECU.device", sender=self, action="interrupt", vendor=self.devices[self.active_device][0], product=self.devices[self.active_device][1], serial=self.active_device)

	def ErrorPanelHandler(self, action):
		if action == "cleardtc":
			self.dtccountl.SetLabel("   DTC Count: --")
			self.statusbar.OnSize(None)

	def USBMonitorHandler(self, action, vendor, product, serial):
		dirty = False
		if action == "add":
			wx.LogMessage("Adding device (%s : %s : %s)" % (vendor, product, serial))
			if not serial in self.devices:
				self.devices[serial] = (vendor, product)
				dirty = True
		elif action =="remove":
			wx.LogMessage("Removing device (%s : %s : %s)" % (vendor, product, serial))
			if serial in self.devices:
				if serial == self.active_device:
					dispatcher.send(signal="HondaECU.device", sender=self, action="deactivate", vendor=vendor, product=product, serial=serial)
					self.__deactivate()
				del self.devices[serial]
				dirty = True
		if len(self.devices) > 0:
			if not self.active_device:
				self.active_device = list(self.devices.keys())[0]
				dispatcher.send(signal="HondaECU.device", sender=self, action="activate", vendor=vendor, product=product, serial=serial)
				dirty = True
		else:
			if not self.args.motoamerica:
				self.__clear_widgets()
		if dirty:
			self.m_devices.Clear()
			for serial in self.devices:
				self.m_devices.Append(self.devices[serial][0] + " : " + self.devices[serial][1] + " : " + serial)
			if self.active_device:
				self.m_devices.SetSelection(list(self.devices.keys()).index(self.active_device))

	def __clear_widgets(self):
		self.ecmidl.SetLabel("")
		self.flashcountl.SetLabel("")
		self.dtccountl.SetLabel("")
		self.statusicon.SetBitmap(self.statusicons[0])
		self.statusicon.Show(False)
		self.statusbar.OnSize(None)

	def KlineWorkerHandler(self, info, value):
		if info == "state":
			if not self.args.motoamerica:
				self.__clear_widgets()
				if value[0] in [0,12]:
					self.statusicon.SetBitmap(self.statusicons[0])
				elif value[0] in [1]:
					self.statusicon.SetBitmap(self.statusicons[1])
				elif value[0] in [10]:
					self.statusicon.SetBitmap(self.statusicons[3])
				else:
					self.ecmidl.SetLabel("   ECM ID: -- -- -- -- --")
					self.flashcountl.SetLabel("   Flash Count: --")
					self.dtccountl.SetLabel("   DTC Count: --")
					self.statusicon.SetBitmap(self.statusicons[2])
				self.statusicon.SetToolTip(wx.ToolTip("state: %s" % (value[1])))
				self.statusicon.Show(True)
				self.statusbar.OnSize(None)
		elif info == "ecmid":
			if not self.args.motoamerica:
				self.ecmidl.SetLabel("   ECM ID: %s" % " ".join(["%02x" % i for i in value]))
				self.statusbar.OnSize(None)
			if value in ECM_IDs:
				self.m_ecu.SetLabel("%s (%s) - %s" % (ECM_IDs[value]["model"],ECM_IDs[value]["year"],ECM_IDs[value]["pn"]))
				if "checksum" in ECM_IDs[value]:
					self.flashp.checksum.SetValue(ECM_IDs[value]["checksum"])
				if "offset" in ECM_IDs[value]:
					self.offset.checksum.SetValue(ECM_IDs[value]["offset"])
			else:
				self.m_ecu.SetLabel("Unknown")
			self.utilpanel.Layout()
		elif info == "flashcount":
			if not self.args.motoamerica:
				self.flashcountl.SetLabel("   Flash Count: %d" % value)
				self.statusbar.OnSize(None)
		elif info == "dtccount":
			if not self.args.motoamerica:
				self.dtccountl.SetLabel("   DTC Count: %d" % value)
				self.statusbar.OnSize(None)
			if value > 0:
				self.errorp.resetbutton.Enable(True)
			else:
				self.errorp.resetbutton.Enable(False)
				self.errorp.errorlist.DeleteAllItems()
		elif info == "dtc":
			if not self.args.motoamerica:
				self.errorp.errorlist.DeleteAllItems()
				self.errorp.Layout()
			for code in value[hex(0x74)]:
				self.errorp.errorlist.Append([code, DTC[code] if code in DTC else "Unknown", "current"])
			for code in value[hex(0x73)]:
				self.errorp.errorlist.Append([code, DTC[code] if code in DTC else "Unknown", "past"])
