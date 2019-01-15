[![Build Status](https://ci.appveyor.com/api/projects/status/rigvo8jwvgaxcbtp?svg=true)](https://ci.appveyor.com/project/RyanHope/hondaecu)

### Please Donate!

Research and development takes time and money. Since it is my goal to keep this project opensource, if you find this project useful, please consider donating.

[![paypal](https://www.paypalobjects.com/en_US/i/btn/btn_donateCC_LG.gif)](https://www.paypal.com/cgi-bin/webscr?cmd=_s-xclick&hosted_button_id=XL3H864LE567E)

## Software

### Windows

HondaECU for Windows requires the libusbK driver. Download [Zadig](https://zadig.akeo.ie/) and use it to install the libusbK driver. There is a usage guide on the Zadig website if you need help using it. Once libusbK is installed for your FTDI device, download the latest release of HondaECU.exe.


#### From source

 https://conda.io/miniconda.html

 https://sourceforge.net/projects/picusb/files/libftdi1-1.4git_devkit_x86_x64_14June2018.zip

 ```
pip install pylibftdi wxPython pydispatcher
 ```

## Checksums

| Model        | Year      | ROM Size | Keihin Code    | Checksum Address | CPU    |
|--------------|-----------|----------|----------------|------------------|--------|
| MSX125       | 2014      | 48 kb    | 38770-K26-911  |                  | M16C   |
| MSX125       | 2017      | 64 kb    | 38770-K26-B13  |                  | M16C   |
| CBR250R      | 2012      | 56 kb    | 38770-KYJ-971  | DFEF             |        |
| CBR250R HRC  | 2014      | 256 kb   | 38770-K33-R51  | 18FFE            |        |
| CBR250RR HRC | 2017      | 1024 kb  | 38770-K64-R02  | 7FFF8            |        |
| CRF250R      | 2011      | 256 kb   | 38770-KRN-E52  | 1FFFA            |        |
| CRF250R      | 2013      | 256 kb   | 38770-KRN-E73  | 18FFE            |        |
| CRF250R      | 2014      | 256 kb   | 38770-KRN-E82  | 19FFE            |        |
| CRF250R      | 2015      | 256 kb   | 38770-KRN-E92  | 19FFE            |        |
| NSF250R      | 2013      | 256 kb   | 38770-NX7-033  | 18FFE            |        |
| VTR250       | 2015      | 256 kb   | 38770-KFK-632  | 3FFF8            |        |
| CBR300R HRC  | 2014      | 256 kb   | 38770-K33-R01  | 18FFE            |        |
| CB300        | 2008-2012 | 56 kb    | 38770-KVK-xxx  | DFEF             |        |
| SH300        | 2009      | 56 kb    | 38770-KTW-901  | DFEF             |        |
| XRE 300      | 2009-2012 | 56 kb    | 38770-KWT-xxx  | DFEF             |        |
| CRF450R      | 2009      | 256 kb   | 38770-MEN-E21  | 1FFFA            |        |
| CRF450R      | 2011      | 256 kb   | 38770-MEN-E52  | 1FFFA            |        |
| CRF450R      | 2014      | 256 kb   | 38770-MEN-A73  | 19FFE            |        |
| CB500R       | 2014      | 256 kb   | 38770-MGZ-B01  | 3FFF8            | PPC    |
| CBR600RR     | 2007-2016 | 256 kb   | 38770-Mxx-xxx  | 3FFF8            | M32R   |
| CB600F       | 2010      | 256 kb   | 38770-MGM-B11  | 3FFF8            |        |
| CB650F       | 2015      | 256 kb   | 38770-MJE-B41  | 3FFF8            |        |
| CB600F/R     | 2010-2011 | 256 kb   | 38770-MFG-Bxx- | 3FFF8            |        |
| Transalp700  | 2013      | 256 kb   | 38770-MFF-B01  | 3FFF8            |        |
| Shadow 750   | 2009      | 256 kb   | 38770-MGE-B21  | 3FFF8            |        |
| NC750X       | 2015      | 256 kb   | 38770-Mxx-xxx  | 3FFF8            |        |
| NC750S       | 2016      | 512 kb   | 38770-MJL-D72  | 7FFF8            |        |
| VFR800F      | 2015      | 512 kb   | 38770-MJM-J13  | 7FFF8            |        |
| CB1000R      | 2008-2016 | 256 kb   | 38770-Mxx-xxx  | 3FFF8            |        |
| CBR1000RR    | 2004-2005 | 256 kb   | 38770-MEL-xxx  | 3FFFC            | M32R   |
| CBR1000RR    | 2006-2016 | 256 kb   | 38770-Mxx-xxx  | 3FFF8            | M32R   |
| CBR1000RR    | 2017      | 1024 kb  | 38770-MKFA-D72 | FFFF8            |        |
| VFR1200F     | 2010      | 1024 kb  | 38770-MGE-D02  | 7FFF8            |        |
| GL1800       | 2015      | 256 kb   | 38770-MJK-J21  | 3FFF8            |        |

## Hardware

The easiest way to talk to the ECU via the k-line is a USB to serial (TTL) converter,
and a serial to k-line converter. This code assumes you are using a FTDI based USB to
serial converter though others may work. I used a FTDI Friend from [Adafruit!](https://www.adafruit.com/product/284).
For the serial to k-line converter I used the schematic below since it contained no
special ICs, just common transistors and R/C components that I had laying around the house.

A note about the circuit below, while the PL2303 is a perfectly good USB to serial converter, it does not support bit banging and is not recommended. If you do have one of these converters you will need to use one of the control lines for the init sequence and that complicates things. Just get yourself a FTDI chip, they are cheap.

![kline_interface_1](http://pinoutguide.com/images/upload/pinout_117944425_image.png)

A much nicer looking k-line to serial converter that I plan on implementing next is
this one that uses one that uses optocouplers to keep the bike power isolated from
your electronics.

![kline_interface_2](http://projects.gonzos.net/wp-content/uploads/2017/04/CTX-kline-interface-1024x514.png)


### Bench Harness

If you make a bench harness to work with your ECU you will need a 2.5 amp power supply. The ECU will power-up with a 2.0 amp power supply but transfer rates will be slow and you will get CRC errors.

## Tuning Software

* http://www.tunerpro.net
* http://www.romraider.com
* https://www.evc.de/en/product/ols/software


## General Resources

* http://forum.pgmfi.org/index.php
* http://ecuhacking.activeboard.com
* http://www.motorsport-brix.de
* https://gonzos.net/projects/ctx-obd
* http://opengarages.org/handbook/ebook
