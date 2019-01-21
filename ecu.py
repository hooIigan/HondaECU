from __future__ import division
from pylibftdi import Device
from ctypes import *
import struct
import time
import binascii
import sys
import platform
import os
import math
import code

ECM_IDs = {
	b"\x01\x00\x2b\x01\x01": {"model":"CBR1000RR","year":"2006-2007","pn":"38770-MEL-D21","checksum":"0x3fff8"},
	b"\x01\x00\x2b\x04\x02": {"model":"CBR1000RR","year":"2006-2007","pn":"38770-MEL-A22","checksum":"0x3fff8"},
	b"\x01\x00\x5e\x04\x01": {"model":"CBR1000RR","year":"2008-2011","pn":"38770-MFL-671","checksum":"0x3fff8"},
	b"\x01\x00\xf3\x04\x01": {"model":"CBR1000RR","year":"2012-2013","pn":"38770-MGP-A01","checksum":"0x3fff8"},
	b"\x01\x01\x83\x01\x01": {"model":"CBR1000RR","year":"2014-2016","pn":"38770-MGP-D62","checksum":"0x3fff8"},
	b"\x01\x01\x83\x04\x01": {"model":"CBR1000RR","year":"2014-2016","pn":"38770-MGP-A92","checksum":"0x3fff8"},
	b"\x01\x01\x25\x05\x01": {"model":"CBR500R","year":"2013-2016","pn":"38770-MGZ-A03","checksum":"0x3fff8"},
	b"\x01\x01\x25\x0b\x01": {"model":"CBR500R","year":"2013-2016","pn":"38770-MGZ-C02","checksum":"0x3fff8"},
	b"\x01\x01\x25\x01\x01": {"model":"CBR500R","year":"2013-2016","pn":"38770-MGZ-D02","checksum":"0x3fff8"}
	# b"\x01\x01\x25\x05\x01": ("CBR500R","2017-2018","38770-MJW-AQ1"},
	# b"\x01\x01\xf2\x01\x01": {"model":"CB500F","year":"2016","pn":"38770-MJW-D51","checksum":"0x3fff8"}
	# b"\x01\x01\x28\x01\x01": {"model":"CB500X","?2013?","?"},
	# b"\x01\x01\xde\x01\x01": {"model":"CB500X","?2016?","?"}
}

def find_compat_bins(base, ecmid):
	bins = {}
	for id in ECM_IDs:
		if id[:3] == ecmid[:3]:
			p = os.path.join(base,ECM_IDs[id]["model"]+"_"+ECM_IDs[id]["pn"].split("-")[1]+"_"+ECM_IDs[id]["year"],ECM_IDs[id]["pn"]+".bin")
			if os.path.isfile(p):
				bins[id] = ECM_IDs[id]
				bins[id]["bin"] = p
	return bins

DTC = {
	"01-01": "MAP sensor circuit low voltage",
	"01-02": "MAP sensor circuit high voltage",
	"02-01": "MAP sensor performance problem",
	"07-01": "ECT sensor circuit low voltage",
	"07-02": "ECT sensor circuit high voltage",
	"08-01": "TP sensor circuit low voltage",
	"08-02": "TP sensor circuit high voltage",
	"09-01": "IAT sensor circuit low voltage",
	"09-02": "IAT sensor circuit high voltage",
	"11-01": "VS sensor no signal",
	"12-01": "No.1 primary injector circuit malfunction",
	"13-01": "No.2 primary injector circuit malfunction",
	"14-01": "No.3 primary injector circuit malfunction",
	"15-01": "No.4 primary injector circuit malfunction",
	"16-01": "No.1 secondary injector circuit malfunction",
	"17-01": "No.2 secondary injector circuit malfunction",
	"18-01": "CMP sensor no signal",
	"19-01": "CKP sensor no signal",
	"21-01": "0₂ sensor malfunction",
	"23-01": "0₂ sensor heater malfunction",
	"25-02": "Knock sensor circuit malfunction",
	"25-03": "Knock sensor circuit malfunction",
	"29-01": "IACV circuit malfunction",
	"33-02": "ECM EEPROM malfunction",
	"34-01": "ECV POT low voltage malfunction",
	"34-02": "ECV POT high voltage malfunction",
	"35-01": "EGCA malfunction",
	"48-01": "No.3 secondary injector circuit malfunction",
	"49-01": "No.4 secondary injector circuit malfunction",
	"51-01": "HESD linear solenoid malfunction",
	"54-01": "Bank angle sensor circuit low voltage",
	"54-02": "Bank angle sensor circuit high voltage",
	"56-01": "Knock sensor IC malfunction",
	"86-01": "Serial communication malfunction"
}

def format_read(location):
	tmp = struct.unpack(">4B",struct.pack(">I",location))
	return [tmp[1], tmp[3], tmp[2]]

def checksum8bitHonda(data):
	return ((sum(bytearray(data)) ^ 0xFF) + 1) & 0xFF

def checksum8bit(data):
	return 0xff - ((sum(bytearray(data))-1) >> 8)

def validate_checksums(byts, nbyts, cksum):
	ret = False
	fixed = False
	if cksum > 0 and cksum < nbyts:
		byts[cksum] = checksum8bitHonda(byts[:cksum]+byts[(cksum+1):])
		fixed = True
	ret = checksum8bitHonda(byts)==0
	return ret, fixed, byts

def do_validation(byts, nbyts, cksum=0):
	status = "good"
	ret, fixed, byts = validate_checksums(byts, nbyts, cksum)
	if not ret:
		status = "bad"
	elif fixed:
		status = "fixed"
	return ret, status, byts

def format_message(mtype, data):
	ml = len(mtype)
	dl = len(data)
	msgsize = 0x02 + ml + dl
	msg = mtype + [msgsize] + data
	msg += [checksum8bitHonda(msg)]
	assert(msg[ml] == len(msg))
	return msg, ml, dl

class HondaECU(object):

	def __init__(self, device_id=None, dprint=None, latency=None, baudrate=10400):
		super(HondaECU, self).__init__()
		self.device_id = device_id
		self.dev = None
		self.error = 0
		self.resets = 0
		self.latency = latency
		self.baudrate = baudrate
		if not dprint:
			self.dprint = self.__dprint
		else:
			self.dprint = dprint
		self.reset()

	def __dprint(self, msg):
		sys.stderr.write(msg)
		sys.stderr.write("\n")
		sys.stderr.flush()

	def reset(self):
		if self.dev != None:
			del self.dev
			self.dev = None

		self.dev = Device(self.device_id, auto_detach=(platform.system()!="Windows"))
		self.setup()
		self.starttime = time.time()

	def time(self):
		return time.time() - self.starttime

	def setup(self):
		self.dev.ftdi_fn.ftdi_usb_reset()
		self.dev.ftdi_fn.ftdi_usb_purge_buffers()
		self.dev.ftdi_fn.ftdi_set_line_property(8, 1, 0)
		self.dev.baudrate = self.baudrate
		if self.latency:
			self.dev.ftdi_fn.ftdi_set_latency_timer(self.latency)
		latency = c_ubyte()
		self.dev.ftdi_fn.ftdi_get_latency_timer(byref(latency))

	def _break(self, ms, debug=False):
		self.dev.ftdi_fn.ftdi_set_bitmode(1, 0x01)
		self.dev._write(b'\x00')
		time.sleep(ms)
		self.dev._write(b'\x01')
		self.dev.ftdi_fn.ftdi_set_bitmode(0, 0x00)
		self.dev.flush()

	def wakeup(self):
		self._break(.070)
		time.sleep(.130)

	def ping(self, debug=False):
		return self.send_command([0xfe],[0x72])!=None

	def probe_tables(self, tables=None):
		if not tables:
			tables = [0x10, 0x11, 0x17, 0x20, 0x21, 0x60, 0x61, 0x67, 0x70, 0x71, 0xd0, 0xd1]
		ret = {}
		for t in tables:
			info = self.send_command([0x72], [0x71, t])
			if info:
				if info[3] > 2:
					ret[t] = [info[3],info[2]]
			else:
				return {}
		return ret

	def init(self, debug=False):
		self.wakeup()
		return self.ping(debug)

	def kline_new(self):
		pin_byte = c_ubyte()
		self.dev.ftdi_fn.ftdi_read_pins(byref(pin_byte))
		return (pin_byte.value == 0xff)

	def kline(self, timeout=.05):
		self.dev.flush()
		self.dev._write(b"\x00")
		to = time.time()
		while time.time() - to < timeout:
			tmp = self.dev._read(1)
			if len(tmp) == 1:
				return tmp == b"\x00"
		return False

	def kline_alt(self):
		self.dev.flush()
		self.dev._write(b"\xff")
		return self.dev._read(1) == b"\xff"

	def kline_old(self):
		b = create_string_buffer(2)
		self.dev.ftdi_fn.ftdi_poll_modem_status(b)
		return b.raw[1] & 16 == 0

	def send(self, buf, ml, timeout=.001):
		self.dev.flush()
		msg = "".join([chr(b) for b in buf]).encode("latin1")
		self.dev._write(msg)
		r = len(msg)
		timeout = .05 + timeout * r
		to = time.time()
		while r > 0:
			r -= len(self.dev._read(r))
			if time.time() - to > timeout: return None
		buf = bytearray()
		r = ml+1
		while r > 0:
			tmp = self.dev._read(r)
			r -= len(tmp)
			buf.extend(tmp)
			if time.time() - to > timeout: return None
		r = buf[-1]-ml-1
		while r > 0:
			tmp = self.dev._read(r)
			r -= len(tmp)
			buf.extend(tmp)
			if time.time() - to > timeout: return None
		return buf

	def send_command(self, mtype, data=[], debug=False, retries=1):
		msg, ml, dl = format_message(mtype, data)
		r = 0
		while r <= retries:
			self.dprint("%d > [%s]" % (r, ", ".join(["%02x" % m for m in msg])))
			resp = self.send(msg, ml)
			if resp:
				if checksum8bitHonda(resp[:-1]) == resp[-1]:
					self.dprint("%d < [%s]" % (r, ", ".join(["%02x" % r for r in resp])))
					rmtype = resp[:ml]
					valid = False
					if ml == 3:
						valid = (rmtype[:2] == bytearray(map(lambda x: x | 0x10, mtype[:2])))
					elif ml == 1:
						valid = (rmtype == bytearray(map(lambda x: x & 0xf, mtype)))
					if valid:
						rml = resp[ml:(ml+1)]
						rdl = ord(rml) - 2 - len(rmtype)
						rdata = resp[(ml+1):-1]
						return (rmtype, rml, rdata, rdl)
					else:
						print("shit")
			r += 1

	def detect_ecu_state(self, wakeup=False):
		states = [
			"unknown",				# 0
			"ok",					# 1
			"recover",				# 2
			"recover (old)",		# 3
			"init",					# 4
			"unlock",				# 5
			"erase",				# 6
			"write",				# 7
			"finalize",				# 8
			"incomplete",			# 9
			"reset",				# 10
			"error",				# 11
			"read",					# 12
			"off",					# 13
		]
		state = 0
		if wakeup:
			self.wakeup()
		if self.ping():
			rinfo = self.send_command([0x7b], [0x00, 0x01, 0x01])
			winfo = self.send_command([0x7d], [0x01, 0x01, 0x01])
			if winfo:
				state = 1
			else:
				state = 2
		else:
			einfo = self.send_command([0x7e], [0x01, 0x01, 0x00])
			if einfo:
				if einfo[2][1] == 0x00:
					state = 3
				elif einfo[2][1] == 0x10:
					state = 4
				elif einfo[2][1] == 0x20:
					state = 5
				elif einfo[2][1] == 0x30:
					state = 6
				elif einfo[2][1] == 0x40:
					state = 7
				elif einfo[2][1] == 0x50:
					state = 8
				elif einfo[2][1] == 0x0d:
					state = 9
				elif einfo[2][1] == 0x0f:
					state = 10
				elif einfo[2][1] == 0xfa:
					state = 11
				else:
					print(hex(einfo[2][1]))
			else:
				dinfo = self.send_command([0x82, 0x82, 0x00], [0x00, 0x00, 0x00, 0x08])
				if dinfo:
					state = 12
		if state == 0:
			if not wakeup:
				state, _ = self.detect_ecu_state(wakeup=True)
			elif not self.kline():
				state = 13
		return state, states[state]

	def do_init_recover(self, debug=False):
		self.send_command([0x7b], [0x00, 0x01, 0x01])
		self.send_command([0x7b], [0x00, 0x01, 0x02])
		self.send_command([0x7b], [0x00, 0x01, 0x03])
		self.send_command([0x7b], [0x00, 0x02, 0x76, 0x03, 0x17])
		self.send_command([0x7b], [0x00, 0x03, 0x75, 0x05, 0x13])

	def do_init_write(self, debug=False):
		self.send_command([0x7d], [0x01, 0x01, 0x01])
		self.send_command([0x7d], [0x01, 0x01, 0x02])
		self.send_command([0x7d], [0x01, 0x01, 0x03])
		self.send_command([0x7d], [0x01, 0x02, 0x50, 0x47, 0x4d])
		self.send_command([0x7d], [0x01, 0x03, 0x2d, 0x46, 0x49])

	def do_erase(self, debug=False):
		self.send_command([0x7e], [0x01, 0x02])
		self.send_command([0x7e], [0x01, 0x03, 0x00, 0x00])
		self.send_command([0x7e], [0x01, 0x0b, 0x00, 0x00, 0x00, 0xff, 0xff, 0xff])
		self.send_command([0x7e], [0x01, 0x0e, 0x01, 0x90])
		self.send_command([0x7e], [0x01, 0x01, 0x01])
		self.send_command([0x7e], [0x01, 0x04, 0xff])

	def do_erase_wait(self, debug=False):
		cont = 1
		while cont:
			info = self.send_command([0x7e], [0x01, 0x05])
			if info:
				if info[2][1] == 0x00:
					cont = 0
			else:
				cont = -1
		if cont == 0:
			into = self.send_command([0x7e], [0x01, 0x01, 0x00])

	def do_post_write(self, debug=False):
		self.send_command([0x7e], [0x01, 0x09])
		time.sleep(.5)
		self.send_command([0x7e], [0x01, 0x0a])
		time.sleep(.5)
		self.send_command([0x7e], [0x01, 0x0c])
		time.sleep(.5)
		info = self.send_command([0x7e], [0x01, 0x0d])
		if info: return (info[2][1] == 0x0f)

	def get_faults(self, debug=False):
		faults = {'past':[], 'current':[]}
		for i in range(1,0x0c):
			info_current = self.send_command([0x72],[0x74, i])[2]
			for j in [3,5,7]:
				if info_current[j] != 0:
					faults['current'].append("%02d-%02d" % (info_current[j],info_current[j+1]))
			if info_current[2] == 0:
				break
		for i in range(1,0x0c):
			info_past = self.send_command([0x72],[0x73, i])[2]
			for j in [3,5,7]:
				if info_past[j] != 0:
					faults['past'].append("%02d-%02d" % (info_past[j],info_past[j+1]))
			if info_past[2] == 0:
				break
		return faults

def print_header():
	sys.stdout.write("===================================================\n")

def do_read_flash(ecu, binfile, offset=0, debug=False):
	readsize = 12
	location = offset
	nl = False
	with open(binfile, "wb") as fbin:
		t = time.time()
		size = location
		rate = 0
		while True:
			info = ecu.send_command([0x82, 0x82, 0x00], format_read(location) + [readsize])
			if not info:
				readsize -= 1
				if readsize < 1:
					break
			else:
				fbin.write(info[2])
				fbin.flush()
				location += readsize
				n = time.time()
				if not debug:
					sys.stdout.write(u"\r  %.02fKB @ %s        " % (location/1024.0, "%.02fB/s" % (rate) if rate > 0 else "---"))
					sys.stdout.flush()
					nl = True
					if n-t > 1:
						rate = (location-size)/(n-t)
						t = n
						size = location
	if nl:
		sys.stdout.write("\n")
		sys.stdout.flush()
	return location > offset

def do_write_flash(ecu, byts, debug=False, offset=0):
	writesize = 128
	ossize = len(byts)
	maxi = int(ossize/writesize)
	offseti = int(offset/16)
	i = 0
	w = 0
	t = time.time()
	rate = 0
	size = 0
	nl = False
	done = False
	while i < maxi and not done:
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
		info = ecu.send_command([0x7e], x)
		if ord(info[1]) != 5:
			sys.stdout.write(" error\n")
			sys.exit(1)
		if info[2][1] == 0:
			done = True
		n = time.time()
		if not debug:
			sys.stdout.write(u"\r  %.02fKB of %.02fKB @ %s        " % (w/1024.0, ossize/1024.0, "%.02fB/s" % (rate) if rate > 0 else "---"))
			sys.stdout.flush()
			nl = True
			if n-t > 1:
				rate = (w-size)/(n-t)
				t = n
				size = w
		i += 1
	if nl:
		sys.stdout.write("\n")
		sys.stdout.flush()
	ecu.send_command([0x7e], [0x01, 0x08])
