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

Update @jornamon 2024: 
- Modified to implement the block device extended interface.
- Accepts arbitrary offsets and lengths in readblocks and writeblocks, with partial blocks handling.
- Intercepts ioctl(6) erase command and ioctl(3) to handle the sync command.
- Now it can be used with LittleFS2.
- Implemented a block cache of variable size. LFS2 performs horribly ons SD cards without one.
  It makes many small and misaligned reads and writes, and the cache helps to cope with that.
- The sync method now is more clever and tries to take advantage of multiblock writes.

TODO:
- Multiblock operations. It seems LFS2 does not use them, so it would primarily benefit FAT.
- Multiblock reads can be done:
    a) Implementing a separate method to read several blocks from cache (and SD card if needed),
    which would apparently only benefit FAT.
    b) Implement a read ahead policy with next blocks. LFS2 could benefit from this, 
    but needs to be measured first, bacause given that LFS2 implements wear leveling,
    it cold be that contiguous blocks are not often requested.
- Multiblock writes ideas:
    a) Implementing a separate method to write several blocks to cache (and SD card if needed),
    which would apparently only benefit FAT. A read ahead policy would have a similar result,
    just not completely adjusted for the requested blocks.

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
    def __init__(self, spi, cs, baudrate=1320000, cache_max_size=8, debug=False):
        self.spi = spi
        self.cs = cs

        self.cmdbuf = bytearray(6)
        self.dummybuf = bytearray(512)
        self.tokenbuf = bytearray(1)
        for i in range(512):
            self.dummybuf[i] = 0xFF
        self.dummybuf_memoryview = memoryview(self.dummybuf)

        # Debug. Collect stats to analyze behavior of file system and cache
        self.debug = debug
        if debug:
            self.stats = self.Stats()

        # Cache data structures
        self._tempbuf = bytearray(512)  # Temporary buffer for partial block handling
        self._mvt = memoryview(self._tempbuf)
        self._cache_max_size = cache_max_size
        self._usage_order: list[int] = []
        self._dirty: list[bool] = [False for _ in range(cache_max_size)]
        self._blocks: dict[int, int] = {}
        self._cache: list[bytearray] = [bytearray(512) for _ in range(cache_max_size)]
        self._mvc: list[memoryview] = [memoryview(b) for b in self._cache]
        ####

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
        # print("init_car: sectors", self.sectors)

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
                # print("[SDCard] v2 card")
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

    def get(self, block_num: int, buf: memoryview) -> None:
        """Get a block from cache."""
        if len(buf) != 512:
            raise ValueError("Buffer must be 512 bytes.")

        if self.debug:
            self.stats.register_contiguity("get", block_num)

        if self._cache_max_size == 0:
            self.read_from_sd(block_num, buf)
            return

        uo = self._usage_order
        blocks = self._blocks
        mvc = self._mvc
        mvb = memoryview(buf)
        dirty = self._dirty
        if block_num in blocks:
            if self.debug:
                self.stats.stats["rb_cache_hit"] += 1  # type: ignore
            slot = blocks[block_num]
            mvb[:] = mvc[slot][:]
            uo.remove(block_num)
            uo.append(block_num)
        else:
            if self.debug:
                self.stats.stats["rb_cache_miss"] += 1  # type: ignore
            cache_size = len(blocks)
            if cache_size == self._cache_max_size:
                oldest_block = uo.pop(0)
                slot = blocks[oldest_block]
                if oldest_block != -1 and dirty[slot]:
                    self.write_to_sd(oldest_block, mvc[slot])
                    if self.debug:
                        self.stats.stats["sb_flush"] += 1
                blocks.pop(oldest_block, None)
            else:
                slot = cache_size
            self.read_from_sd(block_num, mvc[slot])
            dirty[slot] = False
            blocks[block_num] = slot
            uo.append(block_num)
            mvb[:] = mvc[slot][:]

    def put(self, block_num: int, buf: memoryview) -> None:
        """Put a block into cache."""
        if len(buf) != 512:
            raise ValueError("Buffer must be 512 bytes.")

        if self.debug:
            self.stats.register_contiguity("put", block_num)

        # No cache
        if self._cache_max_size == 0:
            self.write_to_sd(block_num, buf)
            return

        uo = self._usage_order
        blocks = self._blocks
        mvc = self._mvc
        mvb = memoryview(buf)
        dirty = self._dirty

        if block_num in blocks:
            # Cache hit
            if self.debug:
                self.stats.stats["wb_cache_hit"] += 1
            slot = blocks[block_num]
            mvc[slot][:] = mvb[:]
            self._dirty[slot] = True
            uo.remove(block_num)
            uo.append(block_num)
        else:
            # Cache miss
            if self.debug:
                self.stats.stats["wb_cache_miss"] += 1
            cache_size = len(blocks)
            if cache_size == self._cache_max_size:
                oldest_block = uo.pop(0)
                slot = blocks[oldest_block]
                if oldest_block != -1 and dirty[slot]:
                    self.write_to_sd(oldest_block, mvc[slot])
                    if self.debug:
                        self.stats.stats["sb_flush"] += 1
                blocks.pop(oldest_block, None)
            else:
                slot = cache_size
            mvc[slot][:] = mvb[:]
            dirty[slot] = True
            blocks[block_num] = slot
            uo.append(block_num)

    def sync(self) -> None:
        """Write all dirty blocks to SD card."""
        if self._cache_max_size == 0:
            return
        mvc = self._mvc
        dirty = self._dirty
        # Dumb sync. Write dirty blocks one by one
        # for block_num, slot in self._blocks.items():
        #     if block_num != -1 and dirty[slot]:
        #         self.write_to_sd(block_num, mvc[slot])
        #         dirty[slot] = False

        # Smart sync. Use multiblock writes if possible
        dirty_blocks = [
            (block_num, slot) for block_num, slot in self._blocks.items() if dirty[slot]
        ]
        dirty_blocks.sort(key=lambda x: x[0])

        i = 0
        while i < len(dirty_blocks) - 1:
            if dirty_blocks[i + 1][0] - dirty_blocks[i][0] != 1:
                # Not contiguous blocks
                self.write_to_sd(dirty_blocks[i][0], mvc[dirty_blocks[i][1]])
                dirty[dirty_blocks[i][1]] = False
                i += 1
                if self.debug:
                    self.stats.stats["sb_sync"] += 1
            else:
                # Contiguous blocks
                contiguos_blocks = 0
                if self.cmd(25, dirty_blocks[i][0] * self.cdv, 0) != 0:
                    raise OSError(5)  # EIO
                self.write(_TOKEN_CMD25, mvc[dirty_blocks[i][1]])
                dirty[dirty_blocks[i][1]] = False
                i += 1
                contiguos_blocks += 1
                while (
                    i < len(dirty_blocks) - 1
                    and dirty_blocks[i + 1][0] - dirty_blocks[i][0] == 1
                ):
                    self.write(_TOKEN_CMD25, mvc[dirty_blocks[i][1]])
                    dirty[dirty_blocks[i][1]] = False
                    i += 1
                    contiguos_blocks += 1
                # Last contiguous block
                self.write(_TOKEN_CMD25, mvc[dirty_blocks[i][1]])
                self.write_token(_TOKEN_STOP_TRAN)
                dirty[dirty_blocks[i][1]] = False
                i += 1
                contiguos_blocks += 1
                if self.debug:
                    self.stats.stats["mb_sync"] += contiguos_blocks

    def read_from_sd(self, block_num: int, buf: memoryview) -> None:
        """Read a block from SD card."""
        if self.cmd(17, block_num * self.cdv, 0, release=False) != 0:
            self.cs(1)
            raise OSError(5)  # EIO
        self.readinto(buf)

    def write_to_sd(self, block_num: int, buf: memoryview) -> None:
        """Write a block to SD card."""
        # workaround for shared bus, required for (at least) some Kingston
        # devices, ensure MOSI is high before starting transaction
        self.spi.write(b"\xff")
        if self.cmd(24, block_num * self.cdv, 0) != 0:
            raise OSError(5)
        self.write(_TOKEN_DATA, buf)

    def readblocks(self, block_num, buf, offset=0):

        if offset < 0:
            raise ValueError("readblocks: Offset must be non-negative")

        # Adjust the block number based on the offset
        len_buf = len(buf)
        block_num += offset // 512
        offset %= 512
        nblocks = (offset + len(buf) + 511) // 512
        mvb = memoryview(buf)
        mvt = self._mvt

        if self.debug:
            aligned = offset == 0 and (offset + len_buf) % 512 == 0
            miss_left = offset > 0 and (offset + len_buf) % 512 == 0
            miss_right = offset == 0 and (offset + len_buf) % 512 > 0
            miss_both = offset > 0 and (offset + len_buf) % 512 > 0
            self.stats.collect(
                "rb",
                block_num=block_num,
                length=len_buf,
                nblocks=nblocks,
                aligned=aligned,
                miss_left=miss_left,
                miss_right=miss_right,
                miss_both=miss_both,
            )

        if nblocks == 1:
            # Only one block to read (partial or complete)
            self.get(block_num, mvt)
            mvb[:] = mvt[offset : offset + len_buf]

        else:
            # More than one block to read
            # CMD18: set read address for multiple blocks

            bytes_read = 0

            # Handle the initial partial block write if there's an offset
            if offset > 0:
                self.get(block_num, mvt)
                bytes_from_first_block = 512 - offset
                mvb[0:bytes_from_first_block] = mvt[offset:]
                bytes_read += bytes_from_first_block
                block_num += 1

            # Read full blocks if any
            while bytes_read + 512 <= len_buf:
                self.get(block_num, mvb[bytes_read : bytes_read + 512])
                bytes_read += 512
                block_num += 1

            # Handle the las partial block if needed
            if bytes_read < len_buf:
                self.get(block_num, mvt)
                mvb[bytes_read:] = mvt[: len_buf - bytes_read]

    def writeblocks(self, block_num, buf, offset=0):

        if offset < 0:
            raise ValueError("writeblocks: Offset must be non-negative")

        # Adjust for offset bigger than block size. Is this a thing?
        len_buf = len(buf)
        block_num += offset // 512
        offset %= 512
        nblocks = (offset + len_buf + 511) // 512

        # Determine if the first and last blocks are misaligned
        first_misaligned = offset > 0
        last_misaligned = (offset + len_buf) % 512 > 0

        if self.debug:
            aligned = offset == 0 and (offset + len_buf) % 512 == 0
            miss_left = offset > 0 and (offset + len_buf) % 512 == 0
            miss_right = offset == 0 and (offset + len_buf) % 512 > 0
            miss_both = offset > 0 and (offset + len_buf) % 512 > 0
            self.stats.collect(
                "wb",
                block_num=block_num,
                length=len_buf,
                nblocks=nblocks,
                aligned=aligned,
                miss_left=miss_left,
                miss_right=miss_right,
                miss_both=miss_both,
            )
            # if not aligned:
            #     print("DEBUG misaligned writeblocks: b_num", block_num, "offset", offset, "nblocks", nblocks, "length", len(buf))  # fmt: skip

        mvt = self._mvt
        mvb = memoryview(buf)

        if nblocks == 1:
            if offset == 0 and (offset + len_buf) == 512:
                # Single complete block, no need to read
                self.put(block_num, mvb)
            else:
                # Single partial block, need to read first
                self.get(block_num, mvt)
                mvt[offset : offset + len_buf] = mvb[:]
                self.put(block_num, mvt)
        else:
            bytes_written = 0
            # Handle the initial partial block write if there's an offset
            if first_misaligned > 0:
                self.get(block_num, mvt)
                bytes_for_first_block = 512 - offset
                mvt[offset:] = mvb[0:bytes_for_first_block]
                self.put(block_num, mvt)
                bytes_written += bytes_for_first_block
                block_num += 1

            # Write full blocks if any
            while bytes_written + 512 <= len_buf:
                self.put(block_num, mvb[bytes_written : bytes_written + 512])
                bytes_written += 512
                block_num += 1

            # Handle the last partial block if needed
            if bytes_written < len_buf:
                self.get(block_num, mvt)
                mvt[0 : len_buf - bytes_written] = mvb[bytes_written:]
                self.put(block_num, mvt)

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

    def cache_reset(self, cache_max_size: int) -> None:
        """Reset the cache. This is mainly for testing purposes, to change
        the cache size on the fly during test runs."""
        self._cache_max_size = cache_max_size
        self._usage_order = []
        self._dirty = [False for _ in range(cache_max_size)]
        self._blocks = {}
        self._cache = [bytearray(512) for _ in range(cache_max_size)]
        self._mvc = [memoryview(b) for b in self._cache]

    def show_cache_status(self):
        """Print the cache status."""
        print("-" * 40)
        print("Cache status")
        print(" - Usage order", self._usage_order)
        print(" - Blocks", self._blocks)
        print(" - Dirty", self._dirty)

    class Stats:
        """Collect statistics about readblocks and writeblocks calls for debugging purposes.
        If the driver is instantiated with debug=False (default), this class is not instantiated."""

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
                    "sb_flush": 0,
                    "sb_sync": 0,
                    "mb_sync": 0,
                }
            )

            # Stats for analysing the contiguity of the blocks requested.
            self.contiguity = OrderedDict()
            self.last_block = None
            self.streak = 0

        def collect(
            self,
            op,
            block_num=0,
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

            s = self.stats
            s["samples"] += 1  # type: ignore
            if op == "rb":
                s["rb"] += 1  # type: ignore
                if nblocks == 1:
                    s["rb_single"] += 1  # type: ignore
                    s["rb_single_aligned"] += 1 if aligned else 0  # type: ignore
                    s["rb_single_miss_left"] += 1 if miss_left else 0  # type: ignore
                    s["rb_single_miss_right"] += 1 if miss_right else 0  # type: ignore
                    s["rb_single_miss_both"] += 1 if miss_both else 0  # type: ignore

                    s["rb_single_min"] = min(s["rb_single_min"], length)  # type: ignore
                    s["rb_single_max"] = max(s["rb_single_max"], length)  # type: ignore
                    s["rb_single_avg"] = (
                        s["rb_single_avg"]
                        + (length - s["rb_single_avg"]) / s["rb_single"]
                    )  # fmt: skip  # type: ignore
                else:
                    s["rb_multi"] += 1  # type: ignore
                    s["rb_multi_aligned"] += 1 if aligned else 0  # type: ignore
                    s["rb_multi_miss_left"] += 1 if miss_left else 0  # type: ignore
                    s["rb_multi_miss_right"] += 1 if miss_right else 0  # type: ignore
                    s["rb_multi_miss_both"] += 1 if miss_both else 0  # type: ignore
                    s["rb_multi_min"] = min(s["rb_multi_min"], length)  # type: ignore
                    s["rb_multi_max"] = max(s["rb_multi_max"], length)  # type: ignore
                    s["rb_multi_avg"] = (
                        s["rb_multi_avg"] + (length - s["rb_multi_avg"]) / s["rb_multi"]
                    )  # fmt: skip  # type: ignore
            elif op == "wb":
                s["wb"] += 1  # type: ignore
                if nblocks == 1:
                    s["wb_single"] += 1  # type: ignore
                    s["wb_single_aligned"] += 1 if aligned else 0  # type: ignore
                    s["wb_single_miss_left"] += 1 if miss_left else 0  # type: ignore
                    s["wb_single_miss_right"] += 1 if miss_right else 0  # type: ignore
                    s["wb_single_miss_both"] += 1 if miss_both else 0  # type: ignore

                    s["wb_single_min"] = min(s["wb_single_min"] or 1e5, length)  # type: ignore
                    s["wb_single_max"] = max(s["wb_single_max"] or 0, length)  # type: ignore
                    s["wb_single_avg"] = (
                        s["wb_single_avg"]
                        + (length - s["wb_single_avg"]) / s["wb_single"]
                    )  # fmt: skip  # type: ignore
                else:
                    s["wb_multi"] += 1  # type: ignore
                    s["wb_multi_aligned"] += 1 if aligned else 0  # type: ignore
                    s["wb_multi_miss_left"] += 1 if miss_left else 0  # type: ignore
                    s["wb_multi_miss_right"] += 1 if miss_right else 0  # type: ignore
                    s["wb_multi_miss_both"] += 1 if miss_both else 0  # type: ignore
                    s["wb_multi_min"] = min(s["wb_multi_min"] or 1e9, length)  # type: ignore
                    s["wb_multi_max"] = max(s["wb_multi_max"] or 0, length)  # type: ignore
                    s["wb_multi_avg"] = (
                        s["wb_multi_avg"] + (length - s["wb_multi_avg"]) / s["wb_multi"]
                    )  # fmt: skip  # type: ignore
            elif op == "ioctl":
                if ioctl == 3:
                    s["ioctl(3) sync"] += 1  # type: ignore
                elif ioctl == 4:
                    s["ioctl(4) num_blocks"] += 1  # type: ignore
                elif ioctl == 5:
                    s["ioctl(5) block_size"] += 1  # type: ignore
                elif ioctl == 6:
                    s["ioctl(6) erase_block"] += 1  # type: ignore

            # noqa

        def register_contiguity(self, op, block_num):
            """Registers streaks of contiguous requested blocks"""
            if block_num == self.last_block:
                return
            if self.last_block is not None and block_num == self.last_block + 1:
                self.streak += 1
            else:
                if self.streak > 0:
                    self.contiguity[self.streak] = self.contiguity.get(self.streak, 0) + 1
                self.streak = 1
            self.last_block = block_num

        def end_contiguity(self):
            """Close the last ongoing streak"""
            if self.streak > 0:
                self.contiguity[self.streak] = self.contiguity.get(self.streak, 0) + 1
                self.streak = 0

        def print_contiguity(self):
            """Print block contiguity stats"""
            print("-" * 40)
            print("Contiguity stats")
            print(" - Readblocks")
            print(f"{'Cont. blocks':>12} {'Times':>10} {'Tot. blocks':>12}")
            for k, v in sorted(self.contiguity.items()):
                print(f"{k:>12d} {v:>10d} {k * v:>12d}")
            print()

        def print_stats(self):
            """Prints driver usage stats"""
            print("-" * 40)
            print("SDCard readblocks and writeblocks Stats")
            for key, value in self.stats.items():
                if value is None:
                    value = "N/A"
                print(f"{key:<20}  {value:>10}")
            print("-" * 40)

        def clear(self):
            """Clear stats"""
            self.__init__()
