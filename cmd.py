import os, sys
from ecu import *
from pylibftdi import FtdiError
import hashlib
import requests

def upload_unknown_bin(byts, bmd5, ecmid=None):
	try:
		data = None
		if ecmid:
			data = {"ecmid": ecmid}
		requests.post('http://ptsv2.com/t/ptmengineering/post', data=data, files={'%s.bin' % (bmd5): byts})
	except:
		pass

def HondaECU_CmdLine(args, version, known_bins):

	offset = 0
	binfile = None
	ret = 1
	if args.mode in ["read","write","recover","checksum","upload"]:
		if os.path.isabs(args.binfile):
			binfile = args.binfile
		else:
			binfile = os.path.abspath(os.path.expanduser(args.binfile))
		if args.mode == "read":
			ret = 0
		else:
			fbin = open(binfile, "rb")
			nbyts = os.path.getsize(binfile)
			byts = bytearray(fbin.read(nbyts))
			fbin.close()
			cksum = 0
			if args.mode != "upload" and args.fix_checksum:
				if args.fix_checksum > 0:
					if args.fix_checksum < nbyts:
						cksum = args.fix_checksum
					else:
						sys.stdout.write("Invalid checksum location\n")
						sys.exit(-1)
				else:
					sys.stdout.write("Invalid checksum location\n")
					sys.exit(-1)
			print_header()
			sys.stdout.write("Validating checksum\n")
			ret, status, byts = do_validation(byts, nbyts, cksum)
			if status == "fixed":
				if args.mode == "checksum":
					fbin = open(binfile, "wb")
					fbin.write(byts)
					fbin.close()
					status += " (permanent)"
				else:
					status += " (temporary)"
			sys.stdout.write("  status: %s\n" % (status))
			if (status != "bad") and args.mode in ["recover","write","upload"]:
				ret = 0
			else:
				ret = -1
	else:
		ret = 0

	if ret == 0:

		if args.mode == "upload":
			print_header()
			sys.stdout.write("Uploading binfile\n")
			md5 = hashlib.md5()
			md5.update(byts)
			bmd5 = md5.hexdigest()
			upload_unknown_bin(byts, bmd5)
		else:
			try:
				if args.debug:
					ecu = HondaECU(latency=args.latency, baudrate=args.baudrate)
				else:
					ecu = HondaECU(dprint=lambda x: False, latency=args.latency, baudrate=args.baudrate)
			except FtdiError:
				sys.stderr.write("No flash adapters detected!\n")
				sys.exit(-2)

			if args.mode == "kline":
				f = [ecu.kline, ecu.kline_old, ecu.kline_new, ecu.kline_alt][args.type]
				while True:
					print(f())
				sys.exit(1)

			if not args.skip_power_check:
				if args.mode in ["read"] and ecu.kline():
					print_header()
					sys.stdout.write("Turn off bike\n")
					while ecu.kline():
						time.sleep(0)
					time.sleep(1)
				if not ecu.kline():
					sys.stdout.write("Turn on bike\n")
					while not ecu.kline():
						time.sleep(0)
					time.sleep(1)

			print_header()
			sys.stdout.write("Waking-up ECU\n")
			ecu.wakeup()

			print_header()
			sys.stdout.write("Detecting ECU state\n")
			state, m = ecu.detect_ecu_state()
			sys.stdout.write("  state: %s\n" % (m))

			if state == 1 and args.mode == "log":
				print_header()
				if args.output == None:
					args.output = sys.stdout
				else:
					args.output = open(args.output,"w")
				header = [
					"time","engine_speed",
					"tps_sensor_voltage","tps_sensor_scantool",
					"ect_sensor_voltage","ect_sensor_scantool",
					"iat_sensor_voltage","iat_sensor_scantool",
					"map_sensor_voltage","map_sensor_scantool",
					"battery_voltage","vehicle_speed",
					"injector_duration","ignition_advance"
				]
				u = ">H12BHB"
				h = header
				table = None
				for t in [0x10,0x11,0x17]:
					info = ecu.send_command([0x72], [0x71, t], debug=args.debug)
					if info and len(info[2][2:]) > 0:
						table = t
						break
				if t == 0x11:
					u += "BH"
					h += ["iacv_pulse_count","iacv_command"]
				elif t == 0x17:
					u += "BB"
				args.output.write("%s\n" % "\t".join(h))
				start = time.time()
				while True:
					info = ecu.send_command([0x72], [0x71, table], debug=args.debug)
					now = time.time() - start
					if info and len(info[2][2:]) > 0:
						data = list(struct.unpack(u, info[2][2:]))
						data[1] = data[1]/0xff*5.0
						data[3] = data[3]/0xff*5.0
						data[4] = -40 + data[4]
						data[5] = data[5]/0xff*5.0
						data[6] = -40 + data[6]
						data[7] = data[7]/0xff*5.0
						data[11] = data[11]/10
						data[13] = data[13]/0xffff*265.5
						data[14] = -64 + data[14]/0xff*127.5
						if table == 0x11:
							data[16] = data[16]/0xffff*8.0
						args.output.write("%f\t%s\n" % (now,"\t".join(map(str,data[:9]+data[11:]))))
					else:
						break
			elif (state in [1,2,3]):
				if args.mode == "scan":
					print_header()
					sys.stdout.write("HDS Tables\n")
					for j in range(256):
						info = ecu.send_command([0x72], [0x71, j], debug=args.debug)
						if info and len(info[2][2:]) > 0:
							sys.stdout.write(" %s\t%s\n" % (hex(j), repr([b for b in info[2][2:]])))

				elif args.mode == "faults":
					if args.clear:
						print_header()
						sys.stdout.write("Clearing fault codes\n")
						while True:
							info = ecu.send_command([0x72],[0x60, 0x03], debug=args.debug)[2]
							if info[1] == 0x00:
								break
					print_header()
					faults = ecu.get_faults(debug=args.debug)
					sys.stdout.write("Fault codes\n")
					if len(faults['current']) > 0:
						sys.stdout.write("  Current:\n")
						for code in faults['current']:
							sys.stdout.write("    %s: %s\n" % (code, DTC[code] if code in DTC else "Unknown"))
					if len(faults['past']) > 0:
						sys.stdout.write("  Past:\n")
						for code in faults['past']:
							sys.stdout.write("    %s: %s\n" % (code, DTC[code] if code in DTC else "Unknown"))

				elif args.mode == "read":
					print_header()
					sys.stdout.write("Fetching ECU info\n")
					table0 = ecu.send_command([0x72], [0x71, 0x00])
					ecmid = " ".join(["%02x" % i for i in table0[2][2:7]])
					sys.stdout.write("  ECM ID: %s\n" % (ecmid))
					print_header()
					sys.stdout.write("Security access\n")
					ecu.send_command([0x27],[0xe0, 0x48, 0x65, 0x6c, 0x6c, 0x6f, 0x48, 0x6f], debug=args.debug)
					ecu.send_command([0x27],[0xe0, 0x77, 0x41, 0x72, 0x65, 0x59, 0x6f, 0x75], debug=args.debug)

					print_header()
					sys.stdout.write("Reading ECU\n")
					if args.offset:
						offset = args.offset
					else:
						offset = 0x0
					do_read_flash(ecu, binfile, offset=offset, debug=args.debug)
					print_header()
					sys.stdout.write("Validating checksum\n")
					with open(binfile, "rb") as fbin:
						nbyts = os.path.getsize(binfile)
						byts = bytearray(fbin.read(nbyts))
						_, _, status, _, _ = do_validation(byts, nbyts)
						sys.stdout.write("  status: %s\n" % (status))
						if status == "good":
							md5 = hashlib.md5()
							md5.update(byts)
							bmd5 = md5.hexdigest()
							if bmd5 in known_bins:
								sys.stdout.write("  stock bin detected: %s\n" % (known_bins[bmd5]))
							else:
								upload_unknown_bin(byts, bmd5, ecmid)

				elif args.mode == "write":
					print_header()
					sys.stdout.write("Initializing write process\n")
					ecu.do_init_write(debug=args.debug)

				elif args.mode == "recover":
					print_header()
					sys.stdout.write("Initializing recovery process\n")
					ecu.do_init_recover(debug=args.debug)

					print_header()
					sys.stdout.write("Entering enhanced diagnostic mode\n")
					ecu.send_command([0x72],[0x00, 0xf1], debug=args.debug)
					time.sleep(1)
					ecu.send_command([0x27],[0x00, 0x01, 0x00], debug=args.debug)

			if args.mode in ["write", "recover"] and (state in [1,2,3]):

				print_header()
				sys.stdout.write("Erasing ECU\n")
				time.sleep(14)
				ecu.do_erase(debug=args.debug)
				ecu.do_erase_wait(debug=args.debug)

				print_header()
				sys.stdout.write("Writing ECU\n")
				#ecu.send_command([0x7e], [0x01, 0x01, 0x00])
				#ecu.send_command([0x7e], [0x01, 0xa0, 0x02])
				if args.offset and args.offset >= 0:
					do_write_flash(ecu, byts, offset=args.offset, debug=args.debug)
				else:
					do_write_flash(ecu, byts, debug=args.debug)

				print_header()
				sys.stdout.write("Finalizing write process\n")
				ret = ecu.do_post_write(debug=args.debug)
				status = "bad"
				if ret:
					status = "good"
				sys.stdout.write("  status: %s\n" % status)

	print_header()
	sys.exit(ret)
