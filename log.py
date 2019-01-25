import argparse
import os, sys
import platform
from ecu import *
from pylibftdi import FtdiError

def Main():
	parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
	parser.add_argument('--output', default=None, help="log output file")
	parser.add_argument('--debug', action='store_true', help="turn on debugging output")
	args = parser.parse_args()

	if args.output == None:
		args.output = sys.stdout
	else:
		args.output = open(args.output,"w")

	skip_header = False

	if args.debug:
		ecu = HondaECU()
	else:
		ecu = HondaECU(dprint=lambda x: False)
	while True:
		if ecu.kline():
			ecu.wakeup()
			if ecu.ping():
				if not skip_header:
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
					skip_header = True
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

if __name__ == '__main__':
	Main()
