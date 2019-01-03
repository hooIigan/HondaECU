import argparse

import os, sys
import platform
import urllib.request

binsdb_url = "https://raw.githubusercontent.com/RyanHope/HondaECU/master/bins.md5"

__VERSION__ = "2.0.0_rc4"

class Hex(object):
	def __call__(self, value):
		return int(value, 16)

def Main():
	default_checksum = '0x3fff8'
	default_romsize = 256

	parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
	subparsers = parser.add_subparsers(metavar='mode',dest='mode')

	parser_read = subparsers.add_parser('read', help='read ecu to binfile')
	parser_read.add_argument('binfile', help="name of output binfile")
	parser_read.add_argument('--rom-size', default=-1, type=int, help="size of ecu rom in kilobytes")
	parser_read.add_argument('--offset', help="read offset", type=Hex())

	parser_write = subparsers.add_parser('write', help='write ecu from binfile')
	parser_write.add_argument('binfile', help="name of input binfile")
	parser_write.add_argument('--rom-size', default=default_romsize, type=int, help="size of ecu rom in kilobytes")
	parser_write.add_argument('--fix-checksum', type=Hex(), help="hex location to fix binfile checksum")
	parser_write.add_argument('--skip-bootloader', action='store_true', help="skip writing bootloader")

	parser_recover = subparsers.add_parser('recover', help='recover ecu from binfile')
	parser_recover.add_argument('binfile', help="name of input binfile")
	parser_recover.add_argument('--rom-size', default=default_romsize, type=int, help="size of ecu rom in kilobytes")
	parser_recover.add_argument('--fix-checksum', type=Hex(), help="hex location to fix binfile checksum")
	parser_recover.add_argument('--skip-bootloader', action='store_true', help="skip writing bootloader")

	parser_checksum = subparsers.add_parser('checksum', help='validate binfile checksum')
	parser_checksum.add_argument('binfile', help="name of input binfile")
	parser_checksum.add_argument('--fix-checksum', type=Hex(), help="hex location to fix binfile checksum")
	parser_checksum.add_argument('--skip-bootloader', action='store_true', help="skip checking bootloader")

	parser_scan = subparsers.add_parser('scan', help='scan engine data')

	parser_upload = subparsers.add_parser('upload', help='upload unknown binfile')
	parser_upload.add_argument('binfile', help="name of input binfile")

	parser_faults = subparsers.add_parser('faults', help='read fault codes')
	parser_faults.add_argument('--clear', action='store_true', help="clear fault codes")

	parser_log = subparsers.add_parser('log', help='log engine data')
	parser_log.add_argument('--output', default=None, help="log output file")

	parser_recover = subparsers.add_parser('kline', help='kline tests')
	parser_recover.add_argument('--type', default=0, type=int, choices=[0,1,2,3], help="kline test type")

	db_grp = parser.add_argument_group('debugging options')
	db_grp.add_argument('--debug', action='store_true', help="turn on debugging output")
	db_grp.add_argument('--verbose', action='store_true', help="turn on verbose output")
	db_grp.add_argument('--noredirect', action='store_true', help="dont redirect stdout/stderr")
	db_grp.add_argument('--latency', type=int, help="latency timer")
	db_grp.add_argument('--baudrate', type=int, default=10400, help="baudrate")
	db_grp.add_argument('--skip-power-check', action='store_true', help="skip power check")
	args = parser.parse_args()

	known_bins = {}
	try:
		r = urllib.request.urlopen(binsdb_url)
		for l in r.readlines():
			md5, file = l.decode("ascii").split()
			known_bins[md5] = os.path.split(file)[-1]
	except:
		pass

	if args.mode == None:
		import wx
		from gui import HondaECU_GUI
		if getattr(sys, 'frozen', False) and not (args.debug or args.verbose):
			sys.__stdout__.close()
			sys.__stderr__.close()
			sys.__stdin__.close()
			os.close(0)
			os.close(1)
			os.close(2)
			import win32console as con
			con.FreeConsole()
		app = wx.App()
		gui = HondaECU_GUI(args, __VERSION__, known_bins)
		app.MainLoop()
	else:
		from cmd import HondaECU_CmdLine
		HondaECU_CmdLine(args, __VERSION__, known_bins)

if __name__ == '__main__':
	Main()
