import sys, os
import argparse
import numpy as np
import scipy.stats
from ecu import *

def myround(n, step):
    return ((n - 1) // step + 1) * step

if __name__ == "__main__":
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('binfile', nargs="*", help="name of input binfile")
    args = parser.parse_args()

    contiguous = False

    for fn in args.binfile:
        print("--------------------------------------------------------------")
        print(fn)
        fn = os.path.abspath(os.path.expanduser(fn))
        with open(fn, "rb") as f:
            nbyts = os.path.getsize(fn)
            byts = bytearray(f.read(nbyts))

            # Find possible segment boudaries
            inc = 0x100
            offset = 0x0
            prevnan = False
            cors = []
            possible_boundaries = []
            while offset <= nbyts - inc:
                with np.errstate(invalid='ignore'):
                    cors.append((offset+inc,np.mean(np.corrcoef(np.array(byts[offset:(offset+inc)]).reshape(16,16)))))
                offset += inc
            for i in range(len(cors)-2):
                z = [cors[i-1][1],cors[i][1],cors[i+1][1]]
                if not all([np.isnan(zz) for zz in z]) and not all([not np.isnan(zz) for zz in z]):
                    b = myround(cors[i][0],0x1000)
                    if b not in possible_boundaries:
                        possible_boundaries.append(b)
            possible_boundaries = sorted(possible_boundaries + [nbyts])
            print("")
            print("  Found %d possible segment boundaries." % (len(possible_boundaries)))

            # Eliminate segment boudaries that don't produce segments with valid checksums
            segstart = 0x0
            segments = []
            for b in possible_boundaries:
                if (segstart == 0x0 and b == nbyts) or segstart == nbyts:
                    continue
                if not all([b==0xff for b in byts[segstart:b]]) and checksum8bitHonda(byts[segstart:b]) == 0:
                    segments.append((segstart,b))
                    segstart = b
            ng = len(segments)
            print("    Found %d valid segments:" % (ng))
            for s in segments:
                print("      0x%x:0x%x" % s)
            complete = sum([e-s for s,e in segments]) == nbyts
            if not complete:
                if len(segments) > 0 and not complete:
                    print("    Segments are incomplete.")
                # Look for split segments
                segments = []
                npb = len(possible_boundaries)
                for i in range(npb):
                    for j in range(npb-i-1):
                        a = byts[:possible_boundaries[i]]
                        b = byts[possible_boundaries[i+j+1]:]
                        c = byts[possible_boundaries[i]:possible_boundaries[i+j+1]]
                        if (checksum8bitHonda(a)!=0 and checksum8bitHonda(b)!=0) and not all([b==0xff for b in a]) and not all([b==0xff for b in b]) and checksum8bitHonda(a+b) == 0 and checksum8bitHonda(c) == 0:
                            segments.append((0x0,possible_boundaries[i],possible_boundaries[i+j+1],nbyts,possible_boundaries[i],possible_boundaries[i+j+1]))

                ng = len(segments)
                if ng > 0:
                    print("")
                    print("    Found %d valid split segments:" % (ng))
                    for s in segments:
                        print("      0x%x:0x%x + 0x%x:0x%x, 0x%x:0x%x" % s)

            if len(segments) == 1 and checksum8bitHonda(byts) == 0:
                print("")
                print("  Bin file appears valid!")
