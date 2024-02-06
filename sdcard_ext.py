"""
MicroPython driver for SD cards using SPI bus.

Requires an SPI bus and a CS pin.  Provides readblocks and writeblocks
methods so the device can be mounted as a filesystem.

Example usage on pyboard:

    import pyb, sdcard, os
    sd = sdcard.SDCard(pyb.SPI(1), pyb.Pin.board.X5)
    pyb.mount(sd, '/sd2')
    os.listdir('/')

Example usage on ESP8266:

    import machine, sdcard, os
    sd = sdcard.SDCard(machine.SPI(1), machine.Pin(15))
    os.mount(sd, '/sd')
    os.listdir('/')

@jornamon 2024: 
Modified to implement the block device extended interface.
Accepts offset in readblocks and writeblocks, with partial block handling.
Implements ioctl(6) to handle the block erase command and ioctl(3) to handle the sync command.
Now it can be used with LittleFS2.
This version follows as close as possible the original sdcard.py. It uses a 1-block cache
because it's needed anyway to handle partial reads and writes.
LFS2 does many small reads and writes and does not perform very well.
See sdcard_lfs.py for a little bit more sophisticated version that uses a configurable size cache
that improves performance for LFS2.
Original driver: https://github.com/micropython/micropython-lib/blob/master/micropython/drivers/storage/sdcard/sdcard.py
"""

from micropython import const
import time


_CMD_TIMEOUT = const(100)

_R1_IDLE_STATE = const(1 << 0)
# R1_ERASE_RESET = const(1 << 1)
_R1_ILLEGAL_COMMAND = const(1 << 2)
# R1_COM_CRC_ERROR = const(1 << 3)
# R1_ERASE_SEQUENCE_ERROR = const(1 << 4)
# R1_ADDRESS_ERROR = const(1 << 5)
# R1_PARAMETER_ERROR = const(1 << 6)
_TOKEN_CMD25 = const(0xFC)
_TOKEN_STOP_TRAN = const(0xFD)
_TOKEN_DATA = const(0xFE)


class SDCard:
    def __init__(self, spi, cs, baudrate=1320000, debug=False):
        self.spi = spi
        self.cs = cs

        self.cmdbuf = bytearray(6)
        self.dummybuf = bytearray(512)
        self.tokenbuf = bytearray(1)
        self.cache = bytearray(512)
        self.mv_cache = memoryview(self.cache)
        self.cache_block = -1
        self.cache_dirty = False

        for i in range(512):
            self.dummybuf[i] = 0xFF
        self.dummybuf_memoryview = memoryview(self.dummybuf)

        self.debug = debug
        if debug:
            self.stats = self.Stats()

        # initialise the card
        self.init_card(baudrate)

    def init_spi(self, baudrate):
        try:
            master = self.spi.MASTER
        except AttributeError:
            # on ESP8266
            self.spi.init(baudrate=baudrate, phase=0, polarity=0)
        else:
            # on pyboard
            self.spi.init(master, baudrate=baudrate, phase=0, polarity=0)

    def init_card(self, baudrate):
        # init CS pin
        self.cs.init(self.cs.OUT, value=1)

        # init SPI bus; use low data rate for initialisation
        self.init_spi(100000)

        # clock card at least 100 cycles with cs high
        for i in range(16):
            self.spi.write(b"\xff")

        # CMD0: init card; should return _R1_IDLE_STATE (allow 5 attempts)
        for _ in range(5):
            if self.cmd(0, 0, 0x95) == _R1_IDLE_STATE:
                break
        else:
            raise OSError("no SD card")

        # CMD8: determine card version
        r = self.cmd(8, 0x01AA, 0x87, 4)
        if r == _R1_IDLE_STATE:
            self.init_card_v2()
        elif r == (_R1_IDLE_STATE | _R1_ILLEGAL_COMMAND):
            self.init_card_v1()
        else:
            raise OSError("couldn't determine SD card version")

        # get the number of sectors
        # CMD9: response R2 (R1 byte + 16-byte block read)
        if self.cmd(9, 0, 0, 0, False) != 0:
            raise OSError("no response from SD card")
        csd = bytearray(16)
        self.readinto(csd)
        if csd[0] & 0xC0 == 0x40:  # CSD version 2.0
            self.sectors = ((csd[8] << 8 | csd[9]) + 1) * 1024
        elif csd[0] & 0xC0 == 0x00:  # CSD version 1.0 (old, <=2GB)
            c_size = (csd[6] & 0b11) << 10 | csd[7] << 2 | csd[8] >> 6
            c_size_mult = (csd[9] & 0b11) << 1 | csd[10] >> 7
            read_bl_len = csd[5] & 0b1111
            capacity = (c_size + 1) * (2 ** (c_size_mult + 2)) * (2**read_bl_len)
            self.sectors = capacity // 512
        else:
            raise OSError("SD card CSD format not supported")
        print("init_car: sectors", self.sectors)

        # CMD16: set block length to 512 bytes
        if self.cmd(16, 512, 0) != 0:
            raise OSError("can't set 512 block size")

        # set to high data rate now that it's initialised
        self.init_spi(baudrate)

    def init_card_v1(self):
        for i in range(_CMD_TIMEOUT):
            time.sleep_ms(50)
            self.cmd(55, 0, 0)
            if self.cmd(41, 0, 0) == 0:
                # SDSC card, uses byte addressing in read/write/erase commands
                self.cdv = 512
                # print("[SDCard] v1 card")
                return
        raise OSError("timeout waiting for v1 card")

    def init_card_v2(self):
        for i in range(_CMD_TIMEOUT):
            time.sleep_ms(50)
            self.cmd(58, 0, 0, 4)
            self.cmd(55, 0, 0)
            if self.cmd(41, 0x40000000, 0) == 0:
                self.cmd(
                    58, 0, 0, -4
                )  # 4-byte response, negative means keep the first byte
                ocr = self.tokenbuf[0]  # get first byte of response, which is OCR
                if not ocr & 0x40:
                    # SDSC card, uses byte addressing in read/write/erase commands
                    self.cdv = 512
                else:
                    # SDHC/SDXC card, uses block addressing in read/write/erase commands
                    self.cdv = 1
                print("[SDCard] v2 card")
                return
        raise OSError("timeout waiting for v2 card")

    def cmd(self, cmd, arg, crc, final=0, release=True, skip1=False):
        self.cs(0)

        # create and send the command
        buf = self.cmdbuf
        buf[0] = 0x40 | cmd
        buf[1] = arg >> 24
        buf[2] = arg >> 16
        buf[3] = arg >> 8
        buf[4] = arg
        buf[5] = crc
        self.spi.write(buf)

        if skip1:
            self.spi.readinto(self.tokenbuf, 0xFF)

        # wait for the response (response[7] == 0)
        for i in range(_CMD_TIMEOUT):
            self.spi.readinto(self.tokenbuf, 0xFF)
            response = self.tokenbuf[0]
            if not (response & 0x80):
                # this could be a big-endian integer that we are getting here
                # if final<0 then store the first byte to tokenbuf and discard the rest
                if final < 0:
                    self.spi.readinto(self.tokenbuf, 0xFF)
                    final = -1 - final
                for j in range(final):
                    self.spi.write(b"\xff")
                if release:
                    self.cs(1)
                    self.spi.write(b"\xff")
                return response

        # timeout
        self.cs(1)
        self.spi.write(b"\xff")
        return -1

    def readinto(self, buf):
        self.cs(0)

        # read until start byte (0xff)
        for i in range(_CMD_TIMEOUT):
            self.spi.readinto(self.tokenbuf, 0xFF)
            if self.tokenbuf[0] == _TOKEN_DATA:
                break
            time.sleep_ms(1)
        else:
            self.cs(1)
            raise OSError("timeout waiting for response")

        # read data
        mv = self.dummybuf_memoryview
        if len(buf) != len(mv):
            mv = mv[: len(buf)]
        self.spi.write_readinto(mv, buf)

        # read checksum
        self.spi.write(b"\xff")
        self.spi.write(b"\xff")

        self.cs(1)
        self.spi.write(b"\xff")

    def write(self, token, buf):
        self.cs(0)

        # send: start of block, data, checksum
        self.spi.read(1, token)
        self.spi.write(buf)
        self.spi.write(b"\xff")
        self.spi.write(b"\xff")

        # check the response
        if (self.spi.read(1, 0xFF)[0] & 0x1F) != 0x05:
            self.cs(1)
            self.spi.write(b"\xff")
            return

        # wait for write to finish
        while self.spi.read(1, 0xFF)[0] == 0:
            pass

        self.cs(1)
        self.spi.write(b"\xff")

    def write_token(self, token):
        self.cs(0)
        self.spi.read(1, token)
        self.spi.write(b"\xff")
        # wait for write to finish
        while self.spi.read(1, 0xFF)[0] == 0x00:
            pass

        self.cs(1)
        self.spi.write(b"\xff")

    def rbdevice(self, block_num, buf, offset=0):
        """DEBUG. For testing purposes, read a block from the device, bypassing the cache"""
        self.sync()
        blockbuf = bytearray(512)
        mv_buf = memoryview(buf)
        if self.cmd(17, block_num * self.cdv, 0, release=False) != 0:
            self.cs(1)
            raise OSError(5)  # EIO
        self.readinto(blockbuf)
        mv_buf[:] = blockbuf[offset : offset + len(buf)]

    def readblocks(self, block_num, buf, offset=0):

        if offset < 0:
            raise ValueError("readblocks: Offset must be non-negative")

        # Adjust the block number based on the offset
        len_buf = len(buf)
        block_num += offset // 512
        offset %= 512
        nblocks = (offset + len(buf) + 511) // 512
        mv_buf = memoryview(buf)
        mv_cache = self.mv_cache
        cache_miss = self.cache_block != block_num

        if self.debug:
            aligned = offset == 0 and (offset + len_buf) % 512 == 0
            miss_left = offset > 0 and (offset + len_buf) % 512 == 0
            miss_right = offset == 0 and (offset + len_buf) % 512 > 0
            miss_both = offset > 0 and (offset + len_buf) % 512 > 0
            self.stats.collect(
                "rb",
                length=len_buf,
                nblocks=nblocks,
                aligned=aligned,
                miss_left=miss_left,
                miss_right=miss_right,
                miss_both=miss_both,
                cache_hit=not cache_miss,
            )

            # if not aligned:
            # print("DEBUG misaligned readblocks: b_num", block_num, "offset", offset, "nblocks", nblocks, "length", len(buf))  # fmt: skip

        if nblocks == 1:
            # Only one block to read (partial or complete)
            if cache_miss:
                self.sync()
                # workaround for shared bus, required for (at least) some Kingston
                # devices, ensure MOSI is high before starting transaction
                self.spi.write(b"\xff")
                # CMD17: set read address for single block
                if self.cmd(17, block_num * self.cdv, 0, release=False) != 0:
                    # release the card
                    self.cs(1)
                    raise OSError(5)  # EIO
                # receive the data and release card
                self.readinto(mv_cache)
                # update cache info
                self.cache_block = block_num
                self.cache_dirty = False
            mv_buf[:] = mv_cache[offset : offset + len_buf]

        else:
            # More than one block to read
            # TODO: Implement a cache for multiple blocks, if it's worth it. For LFS2, apparently it's not.
            # CMD18: set read address for multiple blocks

            if self.cmd(18, block_num * self.cdv, 0, release=False) != 0:
                # release the card
                self.cs(1)
                raise OSError(5)  # EIO

            bytes_read = 0

            # Handle the initial partial block write if there's an offset
            if offset > 0:
                self.readinto(mv_cache)
                # update cache info
                self.cache_block = block_num
                self.cache_dirty = False
                bytes_from_first_block = 512 - offset
                mv_buf[0:bytes_from_first_block] = mv_cache[offset:]
                bytes_read += bytes_from_first_block

            # Read full blocks if any
            while bytes_read + 512 <= len_buf:
                self.readinto(mv_buf[bytes_read : bytes_read + 512])
                bytes_read += 512

            # Handle the las partial block if needed
            if bytes_read < len_buf:
                self.readinto(mv_buf[bytes_read:])

            # End the transmission
            if self.cmd(12, 0, 0xFF, skip1=True):
                raise OSError(5)  # EIO

    def writeblocks(self, block_num, buf, offset=0):
        # workaround for shared bus, required for (at least) some Kingston
        # devices, ensure MOSI is high before starting transaction
        self.spi.write(b"\xff")

        if offset < 0:
            raise ValueError("writeblocks: Offset must be non-negative")

        # Adjust the block number based on the offset
        len_buf = len(buf)
        block_num += offset // 512
        offset %= 512
        nblocks = (offset + len_buf + 511) // 512

        # Determine if the first and last blocks are misaligned
        first_misaligned = offset > 0
        last_misaligned = (offset + len_buf) % 512 > 0
        cache_miss = self.cache_block != block_num

        if self.debug:
            aligned = offset == 0 and (offset + len_buf) % 512 == 0
            miss_left = offset > 0 and (offset + len_buf) % 512 == 0
            miss_right = offset == 0 and (offset + len_buf) % 512 > 0
            miss_both = offset > 0 and (offset + len_buf) % 512 > 0
            self.stats.collect(
                "wb",
                length=len_buf,
                nblocks=nblocks,
                aligned=aligned,
                miss_left=miss_left,
                miss_right=miss_right,
                miss_both=miss_both,
                cache_hit=not cache_miss,
            )
            # if not aligned:
            #     print("DEBUG misaligned writeblocks: b_num", block_num, "offset", offset, "nblocks", nblocks, "length", len(buf))  # fmt: skip

        mv_cache = self.mv_cache
        mv_buf = memoryview(buf)

        if nblocks == 1:
            if cache_miss:
                self.readblocks(block_num, mv_cache)
                mv_cache[offset : offset + len_buf] = buf
                self.cache_dirty = True
            else:
                mv_cache[offset : offset + len_buf] = buf
                self.cache_dirty = True

        else:
            # No caching in multiblock writes
            # Consider implementing a cache for multiple blocks if it's worth it
            self.sync()

            bytes_written = 0

            # Cache first and last block if needed
            if first_misaligned:
                self.readblocks(block_num, mv_cache)
                if last_misaligned:
                    # Both first and last blocks are misaligned
                    # Consider preallocate y this is frequent. I doubt it.
                    mv_cache2 = memoryview(bytearray(512))
                    self.readblocks(block_num + nblocks - 1, mv_cache2)
            else:
                if last_misaligned:
                    # Only Last block is misaligned, do not allocate another cache
                    self.readblocks(block_num + nblocks - 1, mv_cache)

            # More than one block to write (partial or complete)
            # CMD25: set write address for first block
            if self.cmd(25, block_num * self.cdv, 0) != 0:
                raise OSError(5)  # EIO

            # Handle the initial partial block write if there's an offset
            if first_misaligned > 0:
                bytes_for_first_block = 512 - offset
                # Update block content
                mv_cache[offset:] = mv_buf[0:bytes_for_first_block]
                self.write(_TOKEN_CMD25, mv_cache)
                bytes_written += bytes_for_first_block
                block_num += 1

            # Write full blocks if any
            while bytes_written + 512 <= len_buf:
                self.write(_TOKEN_CMD25, mv_buf[bytes_written : bytes_written + 512])
                bytes_written += 512
                block_num += 1

            # Handle the last partial block if needed
            if bytes_written < len_buf:
                # Update block content
                if first_misaligned:
                    # Cached block is in cache2
                    mv_cache2[0 : len_buf - bytes_written] = mv_buf[bytes_written:]  # type: ignore
                    self.write(_TOKEN_CMD25, mv_cache2)  # type: ignore
                else:
                    # Cached block is in cache
                    mv_cache[0 : len_buf - bytes_written] = mv_buf[bytes_written:]
                    self.write(_TOKEN_CMD25, mv_cache)

            # End the transation
            self.write_token(_TOKEN_STOP_TRAN)

            # Invalidate ani cache hit after multiblock write for the moment
            # self.cache_block = -1

    def sync(self):
        mv_cache = self.mv_cache
        if self.cache_dirty:
            # Write the cached block to the card
            # CMD24: set write address for single block
            # print("DEBUG sync: Writing cached block", self.cache_block)  # fmt: skip
            if self.debug:
                self.stats.stats["auto_sync"] += 1
            if self.cmd(24, self.cache_block * self.cdv, 0) != 0:
                raise OSError(5)  # EIO
            self.write(_TOKEN_DATA, mv_cache)
            self.cache_dirty = False

    def ioctl(self, op, arg):
        if self.debug:
            self.stats.collect("ioctl", ioctl=op)
        if op == 3:  # sync
            self.sync()
            return 0
        if op == 4:  # get number of blocks
            return self.sectors
        if op == 5:  # get block size in bytes
            return 512
        if op == 6:  # Ersase block, handled by the controller
            return 0

    class Stats:
        """Collect statistics about readblocks and writeblocks calls for debugging purposes"""

        def __init__(self):
            from collections import OrderedDict

            self.show_every = 0
            self.samples = 0
            self.stats = OrderedDict(
                {
                    "samples": 0,
                    "cache_size": 0,
                    "rb": 0,
                    "rb_single": 0,
                    "rb_single_aligned": 0,
                    "rb_single_miss_left": 0,
                    "rb_single_miss_right": 0,
                    "rb_single_miss_both": 0,
                    "rb_single_min": 9999999,
                    "rb_single_max": 0,
                    "rb_single_avg": 0,
                    "rb_multi": 0,
                    "rb_multi_aligned": 0,
                    "rb_multi_miss_left": 0,
                    "rb_multi_miss_right": 0,
                    "rb_multi_miss_both": 0,
                    "rb_multi_min": 9999999,
                    "rb_multi_max": 0,
                    "rb_multi_avg": 0,
                    "rb_cache_hit": 0,
                    "rb_cache_miss": 0,
                    "wb": 0,
                    "wb_single": 0,
                    "wb_single_aligned": 0,
                    "wb_single_miss_left": 0,
                    "wb_single_miss_right": 0,
                    "wb_single_miss_both": 0,
                    "wb_single_min": 9999999,
                    "wb_single_max": 0,
                    "wb_single_avg": 0,
                    "wb_multi": 0,
                    "wb_multi_aligned": 0,
                    "wb_multi_miss_left": 0,
                    "wb_multi_miss_right": 0,
                    "wb_multi_miss_both": 0,
                    "wb_multi_min": 9999999,
                    "wb_multi_max": 0,
                    "wb_multi_avg": 0,
                    "wb_cache_hit": 0,
                    "wb_cache_miss": 0,
                    "ioctl(3) sync": 0,
                    "ioctl(4) num_blocks": 0,
                    "ioctl(5) block_size": 0,
                    "ioctl(6) erase_block": 0,
                    "auto_sync": 0,
                }
            )

        def collect(
            self,
            op,
            length=0,
            nblocks=0,
            aligned=False,
            miss_left=False,
            miss_right=False,
            miss_both=False,
            cache_hit=False,
            ioctl=0,
        ):
            self.samples += 1
            if self.show_every != 0 and self.samples % self.show_every == 0:
                self.print_stats()
            # noqa
            s = self.stats
            s["samples"] += 1
            if op == "rb":
                s["rb"] += 1
                s["rb_cache_hit"] += 1 if cache_hit else 0
                s["rb_cache_miss"] += 1 if not cache_hit else 0
                if nblocks == 1:
                    s["rb_single"] += 1
                    s["rb_single_aligned"] += 1 if aligned else 0
                    s["rb_single_miss_left"] += 1 if miss_left else 0
                    s["rb_single_miss_right"] += 1 if miss_right else 0
                    s["rb_single_miss_both"] += 1 if miss_both else 0

                    s["rb_single_min"] = min(s["rb_single_min"], length)
                    s["rb_single_max"] = max(s["rb_single_max"], length)
                    s["rb_single_avg"] = s["rb_single_avg"] + (length - s["rb_single_avg"]) / s["rb_single"]  # fmt: skip
                else:
                    s["rb_multi"] += 1
                    s["rb_multi_aligned"] += 1 if aligned else 0
                    s["rb_multi_miss_left"] += 1 if miss_left else 0
                    s["rb_multi_miss_right"] += 1 if miss_right else 0
                    s["rb_multi_miss_both"] += 1 if miss_both else 0
                    s["rb_multi_min"] = min(s["rb_multi_min"], length)
                    s["rb_multi_max"] = max(s["rb_multi_max"], length)
                    s["rb_multi_avg"] = s["rb_multi_avg"] + (length - s["rb_multi_avg"]) / s["rb_multi"]  # fmt: skip
            elif op == "wb":
                s["wb"] += 1
                s["wb_cache_hit"] += 1 if cache_hit else 0
                s["wb_cache_miss"] += 1 if not cache_hit else 0
                if nblocks == 1:
                    s["wb_single"] += 1
                    s["wb_single_aligned"] += 1 if aligned else 0
                    s["wb_single_miss_left"] += 1 if miss_left else 0
                    s["wb_single_miss_right"] += 1 if miss_right else 0
                    s["wb_single_miss_both"] += 1 if miss_both else 0

                    s["wb_single_min"] = min(s["wb_single_min"] or 1e5, length)
                    s["wb_single_max"] = max(s["wb_single_max"] or 0, length)
                    s["wb_single_avg"] = s["wb_single_avg"] + (length - s["wb_single_avg"]) / s["wb_single"]  # fmt: skip
                else:
                    s["wb_multi"] += 1
                    s["wb_multi_aligned"] += 1 if aligned else 0
                    s["wb_multi_miss_left"] += 1 if miss_left else 0
                    s["wb_multi_miss_right"] += 1 if miss_right else 0
                    s["wb_multi_miss_both"] += 1 if miss_both else 0
                    s["wb_multi_min"] = min(s["wb_multi_min"] or 1e9, length)
                    s["wb_multi_max"] = max(s["wb_multi_max"] or 0, length)
                    s["wb_multi_avg"] = s["wb_multi_avg"] + (length - s["wb_multi_avg"]) / s["wb_multi"]  # fmt: skip
            elif op == "ioctl":
                if ioctl == 3:
                    s["ioctl(3) sync"] += 1
                elif ioctl == 4:
                    s["ioctl(4) num_blocks"] += 1
                elif ioctl == 5:
                    s["ioctl(5) block_size"] += 1
                elif ioctl == 6:
                    s["ioctl(6) erase_block"] += 1

            # noqa

        def print_stats(self):
            print("-" * 40)
            print("SDCard readblocks and writeblocks Stats")
            for key, value in self.stats.items():
                if value is None:
                    value = "N/A"
                print(f"{key:<20}: {value:>10}")
            print("-" * 40)

        def clear(self):
            self.__init__()
