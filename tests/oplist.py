"""Takes a log output and convert it to a series of readblocks and writebclocks operations
to reproduce and test the read/write consistency"""

import os
from sdcard_lfs import SDCard
from machine import SPI, Pin

SCK = Pin(36)
MOSI = Pin(35)
MISO = Pin(37)
SPI_N = const(2)
CS = Pin(14, Pin.OUT, value=1)
spi = SPI(SPI_N, baudrate=25_000_000, sck=SCK, mosi=MOSI, miso=MISO)
sd = SDCard(
    spi,
    CS,
    cache_max_size=8,
    read_ahead=4,
    eviction_policy="LRUC",
    analytics=True,
    log=True,
    collect=True,
)
sd.a.fslog.max_size = 80

# Create a list of tuples with the relevant information
def log_oplist(sd, log):
    oplist = []
    for line in log.split("\n"):
        if not line or not ("sdcard/rb" in line or "sdcard/wb" in line):
            continue
        parts = line.split()
        operation = parts[0][-3:-1]
        # print(parts[1][:-1])
        block_num = int(parts[1][:-1])
        offset = int(parts[3][:-1])
        len_buf = int(parts[7])
        oplist.append((operation, block_num, offset, len_buf))
        # print((operation, block_num, offset, len_buf))

    sd._cache.sync()
    blocks = {}
    try:
        # Read all the blocks touched by the operations to memory
        block_nums = set([op[1] for op in oplist])
        for block_num in block_nums:
            read_buf = bytearray(512)
            sd.readblocks(block_num, read_buf, 0)
            blocks[block_num] = read_buf
        tempbuf = bytearray(512)
        mvt = memoryview(tempbuf)
        rb = wb = err = 0
        for op in oplist:
            # Perform the operations and check for inconsistencies
            rw, block_num, offset, len_buf = op
            if rw == "wb":
                # Write the same to memory and device
                wb += 1
                block = blocks[block_num]
                mvb = memoryview(block)
                wbuf = os.urandom(len_buf)
                mvb[offset : offset + len_buf] = wbuf
                sd.writeblocks(block_num, wbuf, offset)

            elif rw == "rb":
                # Read from memory and device and compare
                rb += 1
                block = blocks[block_num]
                mvb = memoryview(block)
                rbuf = bytearray(len_buf)
                sd.readblocks(block_num, rbuf, offset)
                if rbuf != mvb[offset : offset + len_buf]:
                    err += 1
                    print(
                        "Inconsistency found at block",
                        block_num,
                        "offset",
                        offset,
                        "len_buf",
                        len_buf,
                    )
                    print("Expected", mvb[offset : offset + len_buf])
                    print("Got", rbuf)
                else:
                    print("ok with op ", op)
    except Exception as e:
        print(f"\nException in operation {op}")

        raise e
    finally:
        sd._cache.show_cache_status()
        sd.a.print_all()
        print(f"Peformed {rb} reads, {wb} writes (Total {rb + wb}), {err} errors")  # type: ignore


log = """
->sdcard/wb: 643765, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643765
->cache/put/hit block num 643765
->sdcard/rb: 643765, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643765
->sdcard/wb: 643766, offset 0, nblocks 1, len_buf 128
->cache/get/miss/full 643766
->block_evictor(2) LRUC, returned [Block(643757, False, [172, 210, 9, 0]), Block(643753, False, [168, 210, 9, 0])]
->cache/get/miss/full evicted blocks before processing [Block(643757, False, [172, 210, 9, 0]), Block(643753, False, [168, 210, 9, 0])]
->cache/get/miss/full evicted blocks after processing [Block(643766, False, [172, 210, 9, 0]), Block(643767, False, [168, 210, 9, 0])]
->cache/get/miss/full cache blocks before reading from device OrderedDict({643760: Block(643760, True, [176, 210, 9, 0]), 643761: Block(643761, True, [176, 210, 9, 0]), 643762: Block(643762, True, [177, 210, 9, 0]), 643764: Block(643764, True, [179, 210, 9, 0]), 643763: Block(643763, True, [178, 210, 9, 0]), 643765: Block(643765, True, [180, 210, 9, 0]), 643766: Block(643766, False, [172, 210, 9, 0]), 643767: Block(643767, False, [168, 210, 9, 0])})
->cache/get/miss/full cache blocks after reading from device OrderedDict({643760: Block(643760, True, [176, 210, 9, 0]), 643761: Block(643761, True, [176, 210, 9, 0]), 643762: Block(643762, True, [177, 210, 9, 0]), 643764: Block(643764, True, [179, 210, 9, 0]), 643763: Block(643763, True, [178, 210, 9, 0]), 643765: Block(643765, True, [180, 210, 9, 0]), 643766: Block(643766, False, [255, 255, 255, 255]), 643767: Block(643767, False, [255, 255, 255, 255])})
->cache/put/hit block num 643766
->sdcard/rb: 643766, offset 0, nblocks 1, len_buf 128
->cache/get/hit 643766
->sdcard/wb: 643766, offset 128, nblocks 1, len_buf 128
->cache/get/hit 643766
->cache/put/hit block num 643766
->sdcard/rb: 643766, offset 128, nblocks 1, len_buf 128
->cache/get/hit 643766
->sdcard/wb: 643766, offset 256, nblocks 1, len_buf 128
->cache/get/hit 643766
->cache/put/hit block num 643766
->sdcard/rb: 643766, offset 256, nblocks 1, len_buf 128
->cache/get/hit 643766
->sdcard/wb: 643766, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643766
->cache/put/hit block num 643766
->sdcard/rb: 643766, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643766
->sdcard/rb: 643766, offset 0, nblocks 1, len_buf 32
->cache/get/hit 643766
->sdcard/wb: 643767, offset 0, nblocks 1, len_buf 128
->cache/get/hit 643767
->cache/put/hit block num 643767
->sdcard/rb: 643767, offset 0, nblocks 1, len_buf 128
->cache/get/hit 643767
->sdcard/wb: 643767, offset 128, nblocks 1, len_buf 128
->cache/get/hit 643767
->cache/put/hit block num 643767
->sdcard/rb: 643767, offset 128, nblocks 1, len_buf 128
->cache/get/hit 643767
->sdcard/wb: 643767, offset 256, nblocks 1, len_buf 128
->cache/get/hit 643767
->cache/put/hit block num 643767
->sdcard/rb: 643767, offset 256, nblocks 1, len_buf 128
->cache/get/hit 643767
->sdcard/wb: 643767, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643767
->cache/put/hit block num 643767
->sdcard/rb: 643767, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643767
->sdcard/wb: 643768, offset 0, nblocks 1, len_buf 128
->cache/get/miss/full 643768
->block_evictor(2) LRUC, not enough clean blocks, syncing
->cache/sync dirty blocks [Block(643760, True, [176, 210, 9, 0]), Block(643761, True, [176, 210, 9, 0]), Block(643762, True, [177, 210, 9, 0]), Block(643763, True, [178, 210, 9, 0]), Block(643764, True, [179, 210, 9, 0]), Block(643765, True, [180, 210, 9, 0]), Block(643766, True, [181, 210, 9, 0]), Block(643767, True, [182, 210, 9, 0])]
->cache/sync dirty block groups [[Block(643760, False, [176, 210, 9, 0]), Block(643761, False, [176, 210, 9, 0]), Block(643762, False, [177, 210, 9, 0]), Block(643763, False, [178, 210, 9, 0]), Block(643764, False, [179, 210, 9, 0]), Block(643765, False, [180, 210, 9, 0]), Block(643766, False, [181, 210, 9, 0]), Block(643767, False, [182, 210, 9, 0])]], blocks OrderedDict({643760: Block(643760, False, [176, 210, 9, 0]), 643761: Block(643761, False, [176, 210, 9, 0]), 643762: Block(643762, False, [177, 210, 9, 0]), 643764: Block(643764, False, [179, 210, 9, 0]), 643763: Block(643763, False, [178, 210, 9, 0]), 643765: Block(643765, False, [180, 210, 9, 0]), 643766: Block(643766, False, [181, 210, 9, 0]), 643767: Block(643767, False, [182, 210, 9, 0])})
->block_evictor(2) LRUC, returned [Block(643760, False, [176, 210, 9, 0]), Block(643761, False, [176, 210, 9, 0])]
->cache/get/miss/full evicted blocks before processing [Block(643760, False, [176, 210, 9, 0]), Block(643761, False, [176, 210, 9, 0])]
->cache/get/miss/full evicted blocks after processing [Block(643768, False, [176, 210, 9, 0]), Block(643769, False, [176, 210, 9, 0])]
->cache/get/miss/full cache blocks before reading from device OrderedDict({643762: Block(643762, False, [177, 210, 9, 0]), 643764: Block(643764, False, [179, 210, 9, 0]), 643763: Block(643763, False, [178, 210, 9, 0]), 643765: Block(643765, False, [180, 210, 9, 0]), 643766: Block(643766, False, [181, 210, 9, 0]), 643767: Block(643767, False, [182, 210, 9, 0]), 643768: Block(643768, False, [176, 210, 9, 0]), 643769: Block(643769, False, [176, 210, 9, 0])})
->cache/get/miss/full cache blocks after reading from device OrderedDict({643762: Block(643762, False, [177, 210, 9, 0]), 643764: Block(643764, False, [179, 210, 9, 0]), 643763: Block(643763, False, [178, 210, 9, 0]), 643765: Block(643765, False, [180, 210, 9, 0]), 643766: Block(643766, False, [181, 210, 9, 0]), 643767: Block(643767, False, [182, 210, 9, 0]), 643768: Block(643768, False, [255, 255, 255, 255]), 643769: Block(643769, False, [255, 255, 255, 255])})
->cache/put/hit block num 643768
->sdcard/rb: 643768, offset 0, nblocks 1, len_buf 128
->cache/get/hit 643768
->sdcard/wb: 643768, offset 128, nblocks 1, len_buf 128
->cache/get/hit 643768
->cache/put/hit block num 643768
->sdcard/rb: 643768, offset 128, nblocks 1, len_buf 128
->cache/get/hit 643768
->sdcard/wb: 643768, offset 256, nblocks 1, len_buf 128
->cache/get/hit 643768
->cache/put/hit block num 643768
->sdcard/rb: 643768, offset 256, nblocks 1, len_buf 128
->cache/get/hit 643768
->sdcard/wb: 643768, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643768
->cache/put/hit block num 643768
->sdcard/rb: 643768, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643768
->sdcard/rb: 643768, offset 0, nblocks 1, len_buf 32
->cache/get/hit 643768
->sdcard/rb: 643767, offset 0, nblocks 1, len_buf 32
->cache/get/hit 643767
->sdcard/rb: 643765, offset 0, nblocks 1, len_buf 32
->cache/get/hit 643765
->sdcard/wb: 643769, offset 0, nblocks 1, len_buf 128
->cache/get/hit 643769
->cache/put/hit block num 643769
->sdcard/rb: 643769, offset 0, nblocks 1, len_buf 128
->cache/get/hit 643769
->sdcard/wb: 643769, offset 128, nblocks 1, len_buf 128
->cache/get/hit 643769
->cache/put/hit block num 643769
->sdcard/rb: 643769, offset 128, nblocks 1, len_buf 128
->cache/get/hit 643769
->sdcard/wb: 643769, offset 256, nblocks 1, len_buf 128
->cache/get/hit 643769
->cache/put/hit block num 643769
->sdcard/rb: 643769, offset 256, nblocks 1, len_buf 128
->cache/get/hit 643769
->sdcard/wb: 643769, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643769
->cache/put/hit block num 643769
->sdcard/rb: 643769, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643769
->sdcard/wb: 643770, offset 0, nblocks 1, len_buf 128
->cache/get/miss/full 643770
->block_evictor(2) LRUC, returned [Block(643762, False, [177, 210, 9, 0]), Block(643764, False, [179, 210, 9, 0])]
->cache/get/miss/full evicted blocks before processing [Block(643762, False, [177, 210, 9, 0]), Block(643764, False, [179, 210, 9, 0])]
->cache/get/miss/full evicted blocks after processing [Block(643770, False, [177, 210, 9, 0]), Block(643771, False, [179, 210, 9, 0])]
->cache/get/miss/full cache blocks before reading from device OrderedDict({643763: Block(643763, False, [178, 210, 9, 0]), 643766: Block(643766, False, [181, 210, 9, 0]), 643768: Block(643768, True, [184, 210, 9, 0]), 643767: Block(643767, False, [182, 210, 9, 0]), 643765: Block(643765, False, [180, 210, 9, 0]), 643769: Block(643769, True, [184, 210, 9, 0]), 643770: Block(643770, False, [177, 210, 9, 0]), 643771: Block(643771, False, [179, 210, 9, 0])})
->cache/get/miss/full cache blocks after reading from device OrderedDict({643763: Block(643763, False, [178, 210, 9, 0]), 643766: Block(643766, False, [181, 210, 9, 0]), 643768: Block(643768, True, [184, 210, 9, 0]), 643767: Block(643767, False, [182, 210, 9, 0]), 643765: Block(643765, False, [180, 210, 9, 0]), 643769: Block(643769, True, [184, 210, 9, 0]), 643770: Block(643770, False, [255, 255, 255, 255]), 643771: Block(643771, False, [255, 255, 255, 255])})
->cache/put/hit block num 643770
->sdcard/rb: 643770, offset 0, nblocks 1, len_buf 128
->cache/get/hit 643770
->sdcard/wb: 643770, offset 128, nblocks 1, len_buf 128
->cache/get/hit 643770
->cache/put/hit block num 643770
->sdcard/rb: 643770, offset 128, nblocks 1, len_buf 128
->cache/get/hit 643770
->sdcard/wb: 643770, offset 256, nblocks 1, len_buf 128
->cache/get/hit 643770
->cache/put/hit block num 643770
->sdcard/rb: 643770, offset 256, nblocks 1, len_buf 128
->cache/get/hit 643770
->sdcard/wb: 643770, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643770
->cache/put/hit block num 643770
->sdcard/rb: 643770, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643770
->sdcard/rb: 643770, offset 0, nblocks 1, len_buf 32
->cache/get/hit 643770
->sdcard/wb: 643771, offset 0, nblocks 1, len_buf 128
->cache/get/hit 643771
->cache/put/hit block num 643771
->sdcard/rb: 643771, offset 0, nblocks 1, len_buf 128
->cache/get/hit 643771
->sdcard/wb: 643771, offset 128, nblocks 1, len_buf 128
->cache/get/hit 643771
->cache/put/hit block num 643771
->sdcard/rb: 643771, offset 128, nblocks 1, len_buf 128
->cache/get/hit 643771
->sdcard/wb: 643771, offset 256, nblocks 1, len_buf 128
->cache/get/hit 643771
->cache/put/hit block num 643771
->sdcard/rb: 643771, offset 256, nblocks 1, len_buf 128
->cache/get/hit 643771
->sdcard/wb: 643771, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643771
->cache/put/hit block num 643771
->sdcard/rb: 643771, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643771
->sdcard/wb: 643772, offset 0, nblocks 1, len_buf 128
->cache/get/miss/full 643772
->block_evictor(2) LRUC, returned [Block(643763, False, [178, 210, 9, 0]), Block(643766, False, [181, 210, 9, 0])]
->cache/get/miss/full evicted blocks before processing [Block(643763, False, [178, 210, 9, 0]), Block(643766, False, [181, 210, 9, 0])]
->cache/get/miss/full evicted blocks after processing [Block(643772, False, [178, 210, 9, 0]), Block(643773, False, [181, 210, 9, 0])]
->cache/get/miss/full cache blocks before reading from device OrderedDict({643768: Block(643768, True, [184, 210, 9, 0]), 643767: Block(643767, False, [182, 210, 9, 0]), 643765: Block(643765, False, [180, 210, 9, 0]), 643769: Block(643769, True, [184, 210, 9, 0]), 643770: Block(643770, True, [185, 210, 9, 0]), 643771: Block(643771, True, [186, 210, 9, 0]), 643772: Block(643772, False, [178, 210, 9, 0]), 643773: Block(643773, False, [181, 210, 9, 0])})
->cache/get/miss/full cache blocks after reading from device OrderedDict({643768: Block(643768, True, [184, 210, 9, 0]), 643767: Block(643767, False, [182, 210, 9, 0]), 643765: Block(643765, False, [180, 210, 9, 0]), 643769: Block(643769, True, [184, 210, 9, 0]), 643770: Block(643770, True, [185, 210, 9, 0]), 643771: Block(643771, True, [186, 210, 9, 0]), 643772: Block(643772, False, [255, 255, 255, 255]), 643773: Block(643773, False, [255, 255, 255, 255])})
->cache/put/hit block num 643772
->sdcard/rb: 643772, offset 0, nblocks 1, len_buf 128
->cache/get/hit 643772
->sdcard/wb: 643772, offset 128, nblocks 1, len_buf 128
->cache/get/hit 643772
->cache/put/hit block num 643772
->sdcard/rb: 643772, offset 128, nblocks 1, len_buf 128
->cache/get/hit 643772
->sdcard/wb: 643772, offset 256, nblocks 1, len_buf 128
->cache/get/hit 643772
->cache/put/hit block num 643772
->sdcard/rb: 643772, offset 256, nblocks 1, len_buf 128
->cache/get/hit 643772
->sdcard/wb: 643772, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643772
->cache/put/hit block num 643772
->sdcard/rb: 643772, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643772
->sdcard/rb: 643772, offset 0, nblocks 1, len_buf 32
->cache/get/hit 643772
->sdcard/rb: 643771, offset 0, nblocks 1, len_buf 32
->cache/get/hit 643771
->sdcard/wb: 643773, offset 0, nblocks 1, len_buf 128
->cache/get/hit 643773
->cache/put/hit block num 643773
->sdcard/rb: 643773, offset 0, nblocks 1, len_buf 128
->cache/get/hit 643773
->sdcard/wb: 643773, offset 128, nblocks 1, len_buf 128
->cache/get/hit 643773
->cache/put/hit block num 643773
->sdcard/rb: 643773, offset 128, nblocks 1, len_buf 128
->cache/get/hit 643773
->sdcard/wb: 643773, offset 256, nblocks 1, len_buf 128
->cache/get/hit 643773
->cache/put/hit block num 643773
->sdcard/rb: 643773, offset 256, nblocks 1, len_buf 128
->cache/get/hit 643773
->sdcard/wb: 643773, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643773
->cache/put/hit block num 643773
->sdcard/rb: 643773, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643773
->sdcard/wb: 643774, offset 0, nblocks 1, len_buf 128
->cache/get/miss/full 643774
->block_evictor(2) LRUC, returned [Block(643767, False, [182, 210, 9, 0]), Block(643765, False, [180, 210, 9, 0])]
->cache/get/miss/full evicted blocks before processing [Block(643767, False, [182, 210, 9, 0]), Block(643765, False, [180, 210, 9, 0])]
->cache/get/miss/full evicted blocks after processing [Block(643774, False, [182, 210, 9, 0]), Block(643775, False, [180, 210, 9, 0])]
->cache/get/miss/full cache blocks before reading from device OrderedDict({643768: Block(643768, True, [184, 210, 9, 0]), 643769: Block(643769, True, [184, 210, 9, 0]), 643770: Block(643770, True, [185, 210, 9, 0]), 643772: Block(643772, True, [187, 210, 9, 0]), 643771: Block(643771, True, [186, 210, 9, 0]), 643773: Block(643773, True, [188, 210, 9, 0]), 643774: Block(643774, False, [182, 210, 9, 0]), 643775: Block(643775, False, [180, 210, 9, 0])})
->cache/get/miss/full cache blocks after reading from device OrderedDict({643768: Block(643768, True, [184, 210, 9, 0]), 643769: Block(643769, True, [184, 210, 9, 0]), 643770: Block(643770, True, [185, 210, 9, 0]), 643772: Block(643772, True, [187, 210, 9, 0]), 643771: Block(643771, True, [186, 210, 9, 0]), 643773: Block(643773, True, [188, 210, 9, 0]), 643774: Block(643774, False, [255, 255, 255, 255]), 643775: Block(643775, False, [255, 255, 255, 255])})
->cache/put/hit block num 643774
->sdcard/rb: 643774, offset 0, nblocks 1, len_buf 128
->cache/get/hit 643774
->sdcard/wb: 643774, offset 128, nblocks 1, len_buf 128
->cache/get/hit 643774
->cache/put/hit block num 643774
->sdcard/rb: 643774, offset 128, nblocks 1, len_buf 128
->cache/get/hit 643774
->sdcard/wb: 643774, offset 256, nblocks 1, len_buf 128
->cache/get/hit 643774
->cache/put/hit block num 643774
->sdcard/rb: 643774, offset 256, nblocks 1, len_buf 128
->cache/get/hit 643774
->sdcard/wb: 643774, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643774
->cache/put/hit block num 643774
->sdcard/rb: 643774, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643774
->sdcard/rb: 643774, offset 0, nblocks 1, len_buf 32
->cache/get/hit 643774
->sdcard/wb: 643775, offset 0, nblocks 1, len_buf 128
->cache/get/hit 643775
->cache/put/hit block num 643775
->sdcard/rb: 643775, offset 0, nblocks 1, len_buf 128
->cache/get/hit 643775
->sdcard/wb: 643775, offset 128, nblocks 1, len_buf 128
->cache/get/hit 643775
->cache/put/hit block num 643775
->sdcard/rb: 643775, offset 128, nblocks 1, len_buf 128
->cache/get/hit 643775
->sdcard/wb: 643775, offset 256, nblocks 1, len_buf 128
->cache/get/hit 643775
->cache/put/hit block num 643775
->sdcard/rb: 643775, offset 256, nblocks 1, len_buf 128
->cache/get/hit 643775
->sdcard/wb: 643775, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643775
->cache/put/hit block num 643775
->sdcard/rb: 643775, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643775
->sdcard/wb: 643776, offset 0, nblocks 1, len_buf 128
->cache/get/miss/full 643776
->block_evictor(2) LRUC, not enough clean blocks, syncing
->cache/sync dirty blocks [Block(643768, True, [184, 210, 9, 0]), Block(643769, True, [184, 210, 9, 0]), Block(643770, True, [185, 210, 9, 0]), Block(643771, True, [186, 210, 9, 0]), Block(643772, True, [187, 210, 9, 0]), Block(643773, True, [188, 210, 9, 0]), Block(643774, True, [189, 210, 9, 0]), Block(643775, True, [190, 210, 9, 0])]
->cache/sync dirty block groups [[Block(643768, False, [184, 210, 9, 0]), Block(643769, False, [184, 210, 9, 0]), Block(643770, False, [185, 210, 9, 0]), Block(643771, False, [186, 210, 9, 0]), Block(643772, False, [187, 210, 9, 0]), Block(643773, False, [188, 210, 9, 0]), Block(643774, False, [189, 210, 9, 0]), Block(643775, False, [190, 210, 9, 0])]], blocks OrderedDict({643768: Block(643768, False, [184, 210, 9, 0]), 643769: Block(643769, False, [184, 210, 9, 0]), 643770: Block(643770, False, [185, 210, 9, 0]), 643772: Block(643772, False, [187, 210, 9, 0]), 643771: Block(643771, False, [186, 210, 9, 0]), 643773: Block(643773, False, [188, 210, 9, 0]), 643774: Block(643774, False, [189, 210, 9, 0]), 643775: Block(643775, False, [190, 210, 9, 0])})
->block_evictor(2) LRUC, returned [Block(643768, False, [184, 210, 9, 0]), Block(643769, False, [184, 210, 9, 0])]
->cache/get/miss/full evicted blocks before processing [Block(643768, False, [184, 210, 9, 0]), Block(643769, False, [184, 210, 9, 0])]
->cache/get/miss/full evicted blocks after processing [Block(643776, False, [184, 210, 9, 0]), Block(643777, False, [184, 210, 9, 0])]
->cache/get/miss/full cache blocks before reading from device OrderedDict({643770: Block(643770, False, [185, 210, 9, 0]), 643772: Block(643772, False, [187, 210, 9, 0]), 643771: Block(643771, False, [186, 210, 9, 0]), 643773: Block(643773, False, [188, 210, 9, 0]), 643774: Block(643774, False, [189, 210, 9, 0]), 643775: Block(643775, False, [190, 210, 9, 0]), 643776: Block(643776, False, [184, 210, 9, 0]), 643777: Block(643777, False, [184, 210, 9, 0])})
->cache/get/miss/full cache blocks after reading from device OrderedDict({643770: Block(643770, False, [185, 210, 9, 0]), 643772: Block(643772, False, [187, 210, 9, 0]), 643771: Block(643771, False, [186, 210, 9, 0]), 643773: Block(643773, False, [188, 210, 9, 0]), 643774: Block(643774, False, [189, 210, 9, 0]), 643775: Block(643775, False, [190, 210, 9, 0]), 643776: Block(643776, False, [255, 255, 255, 255]), 643777: Block(643777, False, [255, 255, 255, 255])})
->cache/put/hit block num 643776
->sdcard/rb: 643776, offset 0, nblocks 1, len_buf 128
->cache/get/hit 643776
->sdcard/wb: 643776, offset 128, nblocks 1, len_buf 128
->cache/get/hit 643776
->cache/put/hit block num 643776
->sdcard/rb: 643776, offset 128, nblocks 1, len_buf 128
->cache/get/hit 643776
->sdcard/wb: 643776, offset 256, nblocks 1, len_buf 128
->cache/get/hit 643776
->cache/put/hit block num 643776
->sdcard/rb: 643776, offset 256, nblocks 1, len_buf 128
->cache/get/hit 643776
->sdcard/wb: 643776, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643776
->cache/put/hit block num 643776
->sdcard/rb: 643776, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643776
->sdcard/rb: 643776, offset 0, nblocks 1, len_buf 32
->cache/get/hit 643776
->sdcard/rb: 643775, offset 0, nblocks 1, len_buf 32
->cache/get/hit 643775
->sdcard/rb: 643773, offset 0, nblocks 1, len_buf 32
->cache/get/hit 643773
->sdcard/rb: 643769, offset 0, nblocks 1, len_buf 32
->cache/get/miss/full 643769
->cache/get/miss/full read ahead avoided
->block_evictor(1) LRUC, returned [Block(643770, False, [185, 210, 9, 0])]
->cache/get/miss/full evicted blocks before processing [Block(643770, False, [185, 210, 9, 0])]
->cache/get/miss/full evicted blocks after processing [Block(643769, False, [185, 210, 9, 0])]
->cache/get/miss/full cache blocks before reading from device OrderedDict({643772: Block(643772, False, [187, 210, 9, 0]), 643771: Block(643771, False, [186, 210, 9, 0]), 643774: Block(643774, False, [189, 210, 9, 0]), 643777: Block(643777, False, [191, 210, 9, 0]), 643776: Block(643776, True, [191, 210, 9, 0]), 643775: Block(643775, False, [190, 210, 9, 0]), 643773: Block(643773, False, [188, 210, 9, 0]), 643769: Block(643769, False, [185, 210, 9, 0])})
->cache/get/miss/full cache blocks after reading from device OrderedDict({643772: Block(643772, False, [187, 210, 9, 0]), 643771: Block(643771, False, [186, 210, 9, 0]), 643774: Block(643774, False, [189, 210, 9, 0]), 643777: Block(643777, False, [191, 210, 9, 0]), 643776: Block(643776, True, [191, 210, 9, 0]), 643775: Block(643775, False, [190, 210, 9, 0]), 643773: Block(643773, False, [188, 210, 9, 0]), 643769: Block(643769, False, [184, 210, 9, 0])})
->sdcard/rb: 643761, offset 0, nblocks 1, len_buf 32
->cache/get/miss/full 643761
->block_evictor(2) LRUC, returned [Block(643772, False, [187, 210, 9, 0]), Block(643771, False, [186, 210, 9, 0])]
->cache/get/miss/full evicted blocks before processing [Block(643772, False, [187, 210, 9, 0]), Block(643771, False, [186, 210, 9, 0])]
->cache/get/miss/full evicted blocks after processing [Block(643761, False, [187, 210, 9, 0]), Block(643762, False, [186, 210, 9, 0])]
->cache/get/miss/full cache blocks before reading from device OrderedDict({643774: Block(643774, False, [189, 210, 9, 0]), 643777: Block(643777, False, [191, 210, 9, 0]), 643776: Block(643776, True, [191, 210, 9, 0]), 643775: Block(643775, False, [190, 210, 9, 0]), 643773: Block(643773, False, [188, 210, 9, 0]), 643769: Block(643769, False, [184, 210, 9, 0]), 643761: Block(643761, False, [187, 210, 9, 0]), 643762: Block(643762, False, [186, 210, 9, 0])})
->cache/get/miss/full cache blocks after reading from device OrderedDict({643774: Block(643774, False, [189, 210, 9, 0]), 643777: Block(643777, False, [191, 210, 9, 0]), 643776: Block(643776, True, [191, 210, 9, 0]), 643775: Block(643775, False, [190, 210, 9, 0]), 643773: Block(643773, False, [188, 210, 9, 0]), 643769: Block(643769, False, [184, 210, 9, 0]), 643761: Block(643761, False, [176, 210, 9, 0]), 643762: Block(643762, False, [177, 210, 9, 0])})
->sdcard/rb: 643745, offset 0, nblocks 1, len_buf 32
->cache/get/miss/full 643745
->block_evictor(2) LRUC, returned [Block(643774, False, [189, 210, 9, 0]), Block(643777, False, [191, 210, 9, 0])]
->cache/get/miss/full evicted blocks before processing [Block(643774, False, [189, 210, 9, 0]), Block(643777, False, [191, 210, 9, 0])]
->cache/get/miss/full evicted blocks after processing [Block(643745, False, [189, 210, 9, 0]), Block(643746, False, [191, 210, 9, 0])]
->cache/get/miss/full cache blocks before reading from device OrderedDict({643776: Block(643776, True, [191, 210, 9, 0]), 643775: Block(643775, False, [190, 210, 9, 0]), 643773: Block(643773, False, [188, 210, 9, 0]), 643769: Block(643769, False, [184, 210, 9, 0]), 643761: Block(643761, False, [176, 210, 9, 0]), 643762: Block(643762, False, [177, 210, 9, 0]), 643745: Block(643745, False, [189, 210, 9, 0]), 643746: Block(643746, False, [191, 210, 9, 0])})
->cache/get/miss/full cache blocks after reading from device OrderedDict({643776: Block(643776, True, [161, 210, 9, 0]), 643775: Block(643775, False, [190, 210, 9, 0]), 643773: Block(643773, False, [188, 210, 9, 0]), 643769: Block(643769, False, [184, 210, 9, 0]), 643761: Block(643761, False, [176, 210, 9, 0]), 643762: Block(643762, False, [177, 210, 9, 0]), 643745: Block(643745, False, [160, 210, 9, 0]), 643746: Block(643746, False, [161, 210, 9, 0])})
->sdcard/wb: 643777, offset 0, nblocks 1, len_buf 128
->cache/get/miss/full 643777
->block_evictor(2) LRUC, returned [Block(643775, False, [190, 210, 9, 0]), Block(643773, False, [188, 210, 9, 0])]
->cache/get/miss/full evicted blocks before processing [Block(643775, False, [190, 210, 9, 0]), Block(643773, False, [188, 210, 9, 0])]
->cache/get/miss/full evicted blocks after processing [Block(643777, False, [190, 210, 9, 0]), Block(643778, False, [188, 210, 9, 0])]
->cache/get/miss/full cache blocks before reading from device OrderedDict({643776: Block(643776, True, [161, 210, 9, 0]), 643769: Block(643769, False, [184, 210, 9, 0]), 643761: Block(643761, False, [176, 210, 9, 0]), 643762: Block(643762, False, [177, 210, 9, 0]), 643745: Block(643745, False, [160, 210, 9, 0]), 643746: Block(643746, False, [161, 210, 9, 0]), 643777: Block(643777, False, [190, 210, 9, 0]), 643778: Block(643778, False, [188, 210, 9, 0])})
->cache/get/miss/full cache blocks after reading from device OrderedDict({643776: Block(643776, True, [161, 210, 9, 0]), 643769: Block(643769, False, [184, 210, 9, 0]), 643761: Block(643761, False, [176, 210, 9, 0]), 643762: Block(643762, False, [177, 210, 9, 0]), 643745: Block(643745, False, [160, 210, 9, 0]), 643746: Block(643746, False, [161, 210, 9, 0]), 643777: Block(643777, False, [255, 255, 255, 255]), 643778: Block(643778, False, [255, 255, 255, 255])})
->cache/put/hit block num 643777
->sdcard/rb: 643777, offset 0, nblocks 1, len_buf 128
->cache/get/hit 643777
->sdcard/wb: 643777, offset 128, nblocks 1, len_buf 128
->cache/get/hit 643777
->cache/put/hit block num 643777
->sdcard/rb: 643777, offset 128, nblocks 1, len_buf 128
->cache/get/hit 643777
->sdcard/wb: 643777, offset 256, nblocks 1, len_buf 128
->cache/get/hit 643777
->cache/put/hit block num 643777
->sdcard/rb: 643777, offset 256, nblocks 1, len_buf 128
->cache/get/hit 643777
->sdcard/wb: 643777, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643777
->cache/put/hit block num 643777
->sdcard/rb: 643777, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643777
->sdcard/wb: 643778, offset 0, nblocks 1, len_buf 128
->cache/get/hit 643778
->cache/put/hit block num 643778
->sdcard/rb: 643778, offset 0, nblocks 1, len_buf 128
->cache/get/hit 643778
->sdcard/wb: 643778, offset 128, nblocks 1, len_buf 128
->cache/get/hit 643778
->cache/put/hit block num 643778
->sdcard/rb: 643778, offset 128, nblocks 1, len_buf 128
->cache/get/hit 643778
->sdcard/wb: 643778, offset 256, nblocks 1, len_buf 128
->cache/get/hit 643778
->cache/put/hit block num 643778
->sdcard/rb: 643778, offset 256, nblocks 1, len_buf 128
->cache/get/hit 643778
->sdcard/wb: 643778, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643778
->cache/put/hit block num 643778
->sdcard/rb: 643778, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643778
->sdcard/rb: 643778, offset 0, nblocks 1, len_buf 32
->cache/get/hit 643778
->sdcard/wb: 643779, offset 0, nblocks 1, len_buf 128
->cache/get/miss/full 643779
->block_evictor(2) LRUC, returned [Block(643769, False, [184, 210, 9, 0]), Block(643761, False, [176, 210, 9, 0])]
->cache/get/miss/full evicted blocks before processing [Block(643769, False, [184, 210, 9, 0]), Block(643761, False, [176, 210, 9, 0])]
->cache/get/miss/full evicted blocks after processing [Block(643779, False, [184, 210, 9, 0]), Block(643780, False, [176, 210, 9, 0])]
->cache/get/miss/full cache blocks before reading from device OrderedDict({643776: Block(643776, True, [161, 210, 9, 0]), 643762: Block(643762, False, [177, 210, 9, 0]), 643745: Block(643745, False, [160, 210, 9, 0]), 643746: Block(643746, False, [161, 210, 9, 0]), 643777: Block(643777, True, [192, 210, 9, 0]), 643778: Block(643778, True, [193, 210, 9, 0]), 643779: Block(643779, False, [184, 210, 9, 0]), 643780: Block(643780, False, [176, 210, 9, 0])})
->cache/get/miss/full cache blocks after reading from device OrderedDict({643776: Block(643776, True, [161, 210, 9, 0]), 643762: Block(643762, False, [177, 210, 9, 0]), 643745: Block(643745, False, [160, 210, 9, 0]), 643746: Block(643746, False, [161, 210, 9, 0]), 643777: Block(643777, True, [192, 210, 9, 0]), 643778: Block(643778, True, [193, 210, 9, 0]), 643779: Block(643779, False, [255, 255, 255, 255]), 643780: Block(643780, False, [255, 255, 255, 255])})
->cache/put/hit block num 643779
->sdcard/rb: 643779, offset 0, nblocks 1, len_buf 128
->cache/get/hit 643779
->sdcard/wb: 643779, offset 128, nblocks 1, len_buf 128
->cache/get/hit 643779
->cache/put/hit block num 643779
->sdcard/rb: 643779, offset 128, nblocks 1, len_buf 128
->cache/get/hit 643779
->sdcard/wb: 643779, offset 256, nblocks 1, len_buf 128
->cache/get/hit 643779
->cache/put/hit block num 643779
->sdcard/rb: 643779, offset 256, nblocks 1, len_buf 128
->cache/get/hit 643779
->sdcard/wb: 643779, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643779
->cache/put/hit block num 643779
->sdcard/rb: 643779, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643779
->sdcard/wb: 643780, offset 0, nblocks 1, len_buf 128
->cache/get/hit 643780
->cache/put/hit block num 643780
->sdcard/rb: 643780, offset 0, nblocks 1, len_buf 128
->cache/get/hit 643780
->sdcard/wb: 643780, offset 128, nblocks 1, len_buf 128
->cache/get/hit 643780
->cache/put/hit block num 643780
->sdcard/rb: 643780, offset 128, nblocks 1, len_buf 128
->cache/get/hit 643780
->sdcard/wb: 643780, offset 256, nblocks 1, len_buf 128
->cache/get/hit 643780
->cache/put/hit block num 643780
->sdcard/rb: 643780, offset 256, nblocks 1, len_buf 128
->cache/get/hit 643780
->sdcard/wb: 643780, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643780
->cache/put/hit block num 643780
->sdcard/rb: 643780, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643780
->sdcard/rb: 643780, offset 0, nblocks 1, len_buf 32
->cache/get/hit 643780
->sdcard/rb: 643779, offset 0, nblocks 1, len_buf 32
->cache/get/hit 643779
->sdcard/wb: 643781, offset 0, nblocks 1, len_buf 128
->cache/get/miss/full 643781
->block_evictor(2) LRUC, returned [Block(643762, False, [177, 210, 9, 0]), Block(643745, False, [160, 210, 9, 0])]
->cache/get/miss/full evicted blocks before processing [Block(643762, False, [177, 210, 9, 0]), Block(643745, False, [160, 210, 9, 0])]
->cache/get/miss/full evicted blocks after processing [Block(643781, False, [177, 210, 9, 0]), Block(643782, False, [160, 210, 9, 0])]
->cache/get/miss/full cache blocks before reading from device OrderedDict({643776: Block(643776, True, [161, 210, 9, 0]), 643746: Block(643746, False, [161, 210, 9, 0]), 643777: Block(643777, True, [192, 210, 9, 0]), 643778: Block(643778, True, [193, 210, 9, 0]), 643780: Block(643780, True, [195, 210, 9, 0]), 643779: Block(643779, True, [194, 210, 9, 0]), 643781: Block(643781, False, [177, 210, 9, 0]), 643782: Block(643782, False, [160, 210, 9, 0])})
->cache/get/miss/full cache blocks after reading from device OrderedDict({643776: Block(643776, True, [161, 210, 9, 0]), 643746: Block(643746, False, [161, 210, 9, 0]), 643777: Block(643777, True, [192, 210, 9, 0]), 643778: Block(643778, True, [193, 210, 9, 0]), 643780: Block(643780, True, [195, 210, 9, 0]), 643779: Block(643779, True, [194, 210, 9, 0]), 643781: Block(643781, False, [255, 255, 255, 255]), 643782: Block(643782, False, [255, 255, 255, 255])})
->cache/put/hit block num 643781
->sdcard/rb: 643781, offset 0, nblocks 1, len_buf 128
->cache/get/hit 643781
->sdcard/wb: 643781, offset 128, nblocks 1, len_buf 128
->cache/get/hit 643781
->cache/put/hit block num 643781
->sdcard/rb: 643781, offset 128, nblocks 1, len_buf 128
->cache/get/hit 643781
->sdcard/wb: 643781, offset 256, nblocks 1, len_buf 128
->cache/get/hit 643781
->cache/put/hit block num 643781
->sdcard/rb: 643781, offset 256, nblocks 1, len_buf 128
->cache/get/hit 643781
->sdcard/wb: 643781, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643781
->cache/put/hit block num 643781
->sdcard/rb: 643781, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643781
->sdcard/wb: 643782, offset 0, nblocks 1, len_buf 128
->cache/get/hit 643782
->cache/put/hit block num 643782
->sdcard/rb: 643782, offset 0, nblocks 1, len_buf 128
->cache/get/hit 643782
->sdcard/wb: 643782, offset 128, nblocks 1, len_buf 128
->cache/get/hit 643782
->cache/put/hit block num 643782
->sdcard/rb: 643782, offset 128, nblocks 1, len_buf 128
->cache/get/hit 643782
->sdcard/wb: 643782, offset 256, nblocks 1, len_buf 128
->cache/get/hit 643782
->cache/put/hit block num 643782
->sdcard/rb: 643782, offset 256, nblocks 1, len_buf 128
->cache/get/hit 643782
->sdcard/wb: 643782, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643782
->cache/put/hit block num 643782
->sdcard/rb: 643782, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643782
->sdcard/rb: 643782, offset 0, nblocks 1, len_buf 32
->cache/get/hit 643782
->sdcard/wb: 643783, offset 0, nblocks 1, len_buf 128
->cache/get/miss/full 643783
->block_evictor(2) LRUC, not enough clean blocks, syncing
->cache/sync dirty blocks [Block(643776, True, [161, 210, 9, 0]), Block(643777, True, [192, 210, 9, 0]), Block(643778, True, [193, 210, 9, 0]), Block(643779, True, [194, 210, 9, 0]), Block(643780, True, [195, 210, 9, 0]), Block(643781, True, [196, 210, 9, 0]), Block(643782, True, [197, 210, 9, 0])]
->cache/sync dirty block groups [[Block(643776, False, [161, 210, 9, 0]), Block(643777, False, [192, 210, 9, 0]), Block(643778, False, [193, 210, 9, 0]), Block(643779, False, [194, 210, 9, 0]), Block(643780, False, [195, 210, 9, 0]), Block(643781, False, [196, 210, 9, 0]), Block(643782, False, [197, 210, 9, 0])]], blocks OrderedDict({643776: Block(643776, False, [161, 210, 9, 0]), 643746: Block(643746, False, [161, 210, 9, 0]), 643777: Block(643777, False, [192, 210, 9, 0]), 643778: Block(643778, False, [193, 210, 9, 0]), 643780: Block(643780, False, [195, 210, 9, 0]), 643779: Block(643779, False, [194, 210, 9, 0]), 643781: Block(643781, False, [196, 210, 9, 0]), 643782: Block(643782, False, [197, 210, 9, 0])})
->block_evictor(2) LRUC, returned [Block(643776, False, [161, 210, 9, 0]), Block(643746, False, [161, 210, 9, 0])]
->cache/get/miss/full evicted blocks before processing [Block(643776, False, [161, 210, 9, 0]), Block(643746, False, [161, 210, 9, 0])]
->cache/get/miss/full evicted blocks after processing [Block(643783, False, [161, 210, 9, 0]), Block(643784, False, [161, 210, 9, 0])]
->cache/get/miss/full cache blocks before reading from device OrderedDict({643777: Block(643777, False, [192, 210, 9, 0]), 643778: Block(643778, False, [193, 210, 9, 0]), 643780: Block(643780, False, [195, 210, 9, 0]), 643779: Block(643779, False, [194, 210, 9, 0]), 643781: Block(643781, False, [196, 210, 9, 0]), 643782: Block(643782, False, [197, 210, 9, 0]), 643783: Block(643783, False, [161, 210, 9, 0]), 643784: Block(643784, False, [161, 210, 9, 0])})
->cache/get/miss/full cache blocks after reading from device OrderedDict({643777: Block(643777, False, [192, 210, 9, 0]), 643778: Block(643778, False, [193, 210, 9, 0]), 643780: Block(643780, False, [195, 210, 9, 0]), 643779: Block(643779, False, [194, 210, 9, 0]), 643781: Block(643781, False, [196, 210, 9, 0]), 643782: Block(643782, False, [197, 210, 9, 0]), 643783: Block(643783, False, [255, 255, 255, 255]), 643784: Block(643784, False, [255, 255, 255, 255])})
->cache/put/hit block num 643783
->sdcard/rb: 643783, offset 0, nblocks 1, len_buf 128
->cache/get/hit 643783
->sdcard/wb: 643783, offset 128, nblocks 1, len_buf 128
->cache/get/hit 643783
->cache/put/hit block num 643783
->sdcard/rb: 643783, offset 128, nblocks 1, len_buf 128
->cache/get/hit 643783
->sdcard/wb: 643783, offset 256, nblocks 1, len_buf 128
->cache/get/hit 643783
->cache/put/hit block num 643783
->sdcard/rb: 643783, offset 256, nblocks 1, len_buf 128
->cache/get/hit 643783
->sdcard/wb: 643783, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643783
->cache/put/hit block num 643783
->sdcard/rb: 643783, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643783
->sdcard/wb: 643784, offset 0, nblocks 1, len_buf 128
->cache/get/hit 643784
->cache/put/hit block num 643784
->sdcard/rb: 643784, offset 0, nblocks 1, len_buf 128
->cache/get/hit 643784
->sdcard/wb: 643784, offset 128, nblocks 1, len_buf 128
->cache/get/hit 643784
->cache/put/hit block num 643784
->sdcard/rb: 643784, offset 128, nblocks 1, len_buf 128
->cache/get/hit 643784
->sdcard/wb: 643784, offset 256, nblocks 1, len_buf 128
->cache/get/hit 643784
->cache/put/hit block num 643784
->sdcard/rb: 643784, offset 256, nblocks 1, len_buf 128
->cache/get/hit 643784
->sdcard/wb: 643784, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643784
->cache/put/hit block num 643784
->sdcard/rb: 643784, offset 384, nblocks 1, len_buf 128
->cache/get/hit 643784
->sdcard/rb: 643784, offset 0, nblocks 1, len_buf 32
->cache/get/hit 643784
->sdcard/rb: 643783, offset 0, nblocks 1, len_buf 32
->cache/get/hit 643783
"""

log_oplist(sd, log)
