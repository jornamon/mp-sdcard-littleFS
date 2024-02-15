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
- Implement the block device extended interface.
- Accepts arbitrary offsets and lengths in readblocks and writeblocks, with partial blocks handling.
- Intercepts ioctl(6) erase command and ioctl(3) to handle the sync command.
- Now it can be used with LittleFS2. Probably with any other file system that uses the block device interface.
- Implemented a block cache of variable size. LFS2 performs horribly on SD cards without one.
  It makes many small and misaligned reads and writes, and the cache helps to cope with that.
- The sync method now is more clever and tries to take advantage of multiblock writes when possible.
- There's now a specific `block_evictor` method to handle the eviction policy. This allows to abstract the eviction
  algorithm and implement different policies for testing or fine tunning. For now, only Least Recently Used (LRU) 
  and Least Recently Used Clean (LRUC) are implemented.
- Implement a configurable Read Ahead feature, which can be set to 1 (no read ahead), or any number of blocks to
  read ahead.
- Debug and analysis features can be now enable / disable through debug_flags kwarg when instantiating the driver.
  Sea docs for details. Be carefull, enabling debug features dramatically slows down the driver.
- The basic usage signature is the same as the original driver, but now it accepts a few more optional arguments to
  configure the cache and debug features.
  
import machine, sdcard_lfs, os
    sd = sdcard_lfs.SDCard(
        machine.SPI(1),
        machine.Pin(15),
        cache_max_size=16,
        read_ahead=1,
        eviction_policy="LRUC",
    )
    os.VfsLfs2.mkfs(sd)   # If not already formated
    os.mount(sd, '/sd')  # If not already mounted
    os.listdir('/')

TODO:
- Pinned blocks. This is a feature that allows to pin blocks in the cache. This is usefull for special blocks accessed frequently.
- Time get and put vs actual device operations to estimate cache overhead.

Original driver: https://github.com/micropython/micropython-lib/blob/master/micropython/drivers/storage/sdcard/sdcard.py
"""

try:
    from typing import Any
except ImportError:
    pass

import time
from micropython import const
from collections import OrderedDict


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


class LRMDict(OrderedDict):
    """An ordered dict with some special features usefull for block cache management:
    - Every Modified item goes by default goes to the end of the dict (most recently modified)
    - move_to_end method moves a specific item to the end of the dictionary.
    - popitem method pops the last item by default, but can also pop the first item.
    """

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        v = self.pop(key)
        self.update({key: v})

    def move_to_end(self, key):
        v = self.pop(key)
        self.update({key: v})

    def popitem(self, last=True):
        if last:
            return super().popitem()
        else:
            k = next(iter(self))
            return k, self.pop(k)


class Block:
    """A class to represent a block in the cache."""

    __slots__ = ["block_num", "dirty", "content"]

    def __init__(self, block_num: int, dirty: bool, content: memoryview):
        self.block_num = block_num
        self.dirty = dirty
        self.content = content

    def __str__(self):
        return f"Block({self.block_num}, {self.dirty})"
        # return f"Block({self.block_num}, {self.dirty}, {list(self.content[:4])})"

    def __repr__(self) -> str:
        return self.__str__()


class Cache:
    """A class to represent a block cache. Now separeted from the SDCard class
    to facilitate device independent testing a potential reuse.
    Cache works only with full blocks. Partial blocks are handled by the Device Driver.
    """

    def __init__(
        self,
        device,
        block_size: int = 512,
        cache_max_size: int = 8,
        eviction_policy: str = "LRUC",
        read_ahead: int = 1,
        **debug_flags,
    ):
        self._block_size = block_size
        self._cache_max_size = cache_max_size
        self._eviction_policy = eviction_policy.upper()
        if read_ahead < 1 or read_ahead > cache_max_size:
            raise ValueError("Read ahead must be between 1 and cache_max_size")
        self._read_ahead = read_ahead
        self._debug_flags = debug_flags
        self.a: Analytics  # type: ignore # Will be populated by the SDCard class

        self._cache: list[bytearray] = [bytearray(block_size) for _ in range(cache_max_size)]
        self._blocks: LRMDict = LRMDict()
        self._device = device

    def block_evictor(self, nblocks: int) -> list[Block]:
        """Selects nblocks blocks to be evicted from cache according to active eviction policy.
        Returns the list of evicted Blocks."""

        blocks = self._blocks
        if self._eviction_policy == "LRU":
            # Least Recently Used
            evicted_blocks = list(blocks.values())[:nblocks]
            # self.a.log(f"->block_evictor({nblocks}) LRU, returned {evicted_blocks}")  # fmt: skip
            return evicted_blocks

        elif self._eviction_policy == "LRUC":
            # Least Recently Used *Clean* block
            clean_blocks = []
            for block in blocks.values():
                if not block.dirty:
                    clean_blocks.append(block)
                    if len(clean_blocks) == nblocks:
                        # self.a.log(f"->block_evictor({nblocks}) LRUC, returned {clean_blocks}")  # fmt: skip
                        return clean_blocks
            # Not enough clean blocks. Sync and return the oldest blocks (now clean)
            # self.a.log(f"->block_evictor({nblocks}) LRUC, not enough clean blocks, syncing")  # fmt: skip
            self.sync()
            evicted_blocks = list(blocks.values())[:nblocks]
            # self.a.log(f"->block_evictor({nblocks}) LRUC, returned {evicted_blocks}")  # fmt: skip
            return evicted_blocks
        else:
            raise ValueError(f"Unknown eviction policy {self._eviction_policy}")

    def get(self, block_num: int, buf: memoryview) -> None:
        """Get a block from cache."""
        if buf and len(buf) != self._block_size:
            raise ValueError(f"Buffer must be {self._block_size} bytes.")

        if self._cache_max_size == 0 and buf is not None:
            # No cache, bypass it
            # Bypass cache creating an adhoc block pointing to the buffer
            self.read_from_device([Block(block_num, False, buf)])
            return
        blocks = self._blocks
        ra = self._read_ahead
        max_size = self._cache_max_size
        mvb = memoryview(buf)

        if block_num in blocks:
            # Cache hit, return result from cache
            # self.a.collect("cache/get/hit")  # fmt: skip
            # self.a.log(f"->cache/get/hit {block_num}")  # fmt: skip

            mvb[:] = blocks[block_num].content[:]
            blocks.move_to_end(block_num)

        else:
            # Cache miss
            # self.a.collect("cache/get/miss")  # fmt: skip

            cache_size = len(blocks)
            if cache_size == self._cache_max_size:
                # self.a.collect("cache/get/miss/full")  # fmt: skip
                # self.a.log(f"->cache/get/miss/full {block_num}")  # fmt: skip

                # Cache is full, evict blocks
                if set(blocks.keys()).intersection(range(block_num, block_num + ra)):
                    # Avoid read ahead if any block to be read ahead is already in the cache.
                    # TODO consider a more sophisticated way to handle this. Worth it?
                    # self.a.log(f"->cache/get/miss/full read ahead avoided")  # fmt: skip
                    # self.a.collect(f"cache/get/miss/full/ra_avoided")  # fmt: skip
                    ra = 1
                evicted_blocks = self.block_evictor(ra)
                for i, block in enumerate(evicted_blocks):
                    if block.dirty:
                        # TODO This check could be eliminated if only LRUC is used. Or any policy that only returns clean blocks.
                        # Consider disabling LRU altogether
                        # Also, could be optimized for multiblock writes if not eliminated.
                        # self.a.log(f"->cache/get/miss/full dirty block evicted, writting to device {block.block_num}")  # fmt: skip
                        self.write_to_device([block])
                    # Update block metadata and get from device
                    blocks.pop(block.block_num)
                    block.dirty = False
                    block.block_num = block_num + i
                    blocks[block.block_num] = block
                self.read_from_device(evicted_blocks)
                # self.a.log(f"->cache/get/miss/full cache blocks after operation {self._blocks}")  # fmt: skip
                mvb[:] = evicted_blocks[0].content[:]
            else:
                # Cache is not full, Create and add new blocks.
                # self.a.collect(f"cache/get/miss/not_full")  # fmt: skip
                # self.a.log(f"->cache/get/miss/not_full block_num {block_num}")  # fmt: skip
                slots = range(
                    cache_size,
                    cache_size + min(ra, max_size - cache_size),
                )
                # self.a.log(f"->cache/get/miss/not_full slots {list(slots)}")  # fmt: skip
                new_blocks = []
                for i, slot in enumerate(slots):
                    b = Block(block_num + i, False, memoryview(self._cache[slot]))
                    new_blocks.append(b)
                    blocks[block_num + i] = b
                self.read_from_device(new_blocks)
                # self.a.log(f"->cache/get/miss/not_full new blocks after operation {new_blocks}")  # fmt: skip
                mvb[:] = new_blocks[0].content[:]

    def put(self, block_num: int, buf: memoryview) -> None:
        """Put a block into cache."""
        if len(buf) != self._block_size:
            raise ValueError(f"Buffer must be {self._block_size} bytes.")

        # self.a.collect("cache/put")  # fmt: skip

        # No cache
        if self._cache_max_size == 0:
            # Bypass cache creating an adhoc block pointing to the buffer
            self.write_to_device([Block(block_num, False, buf)])
            return

        blocks = self._blocks
        mvb = memoryview(buf)

        if block_num in blocks:
            # Cache hit
            # self.a.log(f"->cache/put/hit block num {block_num}")  # fmt: skip
            # self.a.collect("cache/put/hit")  # fmt: skip

            blocks[block_num].content[:] = mvb[:]
            blocks[block_num].dirty = True
            blocks.move_to_end(block_num)
        else:
            # Cache miss

            # self.a.collect("cache/put/miss")  # fmt: skip

            cache_size = len(blocks)
            if cache_size == self._cache_max_size:
                # Cache full, evict one block and write to it
                evicted_block = self.block_evictor(1)[0]
                if evicted_block.dirty:
                    self.write_to_device([evicted_block])

                # self.a.collect("cache/put/miss/full")  # fmt: skip
                # self.a.log(f"->cache/put/miss/full block {block_num}, evicting {evicted_block}, blocks {self._blocks}")  # fmt: skip

                blocks.pop(evicted_block.block_num)
                evicted_block.block_num = block_num
                evicted_block.dirty = True
                evicted_block.content[:] = mvb[:]
                blocks[evicted_block.block_num] = evicted_block
            else:
                # Cache not full, add new block
                slot = cache_size
                blocks[block_num] = Block(block_num, True, memoryview(self._cache[slot]))
                blocks[block_num].content[:] = mvb[:]

                # self.a.collect("cache/put/miss/not_full")  # fmt: skip
                # self.a.log(f"->cache/put/miss/not_full @end {block_num}, slot {slot}, blocks {self._blocks}")  # fmt: skip

    def sync(self) -> None:
        """Write all dirty blocks to SD card.
        Finds dirty blocks, sort them, group them to use multiblock operations if possible.
        and writes them to the device."""

        if self._cache_max_size == 0:
            return

        blocks = self._blocks

        dirty_blocks = sorted(
            (block for block in blocks.values() if block.dirty), key=lambda x: x.block_num
        )
        # self.a.log(f"->cache/sync dirty blocks {dirty_blocks}")  # fmt: skip
        # self.a.collect(f"cache/sync/total")  # fmt: skip
        if not dirty_blocks:
            # self.a.collect(f"cache/sync/nodirtyblocks")  # fmt: skip
            return

        block_groups = [[dirty_blocks[0]]]
        dirty_blocks[0].dirty = False

        # Group contiguous dirty blocks to use multiblock operations
        for block in dirty_blocks[1:]:
            block.dirty = False
            if block.block_num == block_groups[-1][-1].block_num + 1:
                block_groups[-1].append(block)
            else:
                block_groups.append([block])

        for group in block_groups:
            self.write_to_device(group)

        # self.a.log(f"->cache/sync dirty block groups {block_groups}, blocks {self._blocks}")  # fmt: skip

    def read_from_device(self, blocks: list[Block]) -> None:
        """Read blocks fron the device to the cache blocks.
        Uses multiplock operations if possible.
        Read is made into the cache (blocks.content) unless a different buffer is provided.
        This is the method that should be changed if the underlaying device changes."""
        cmd = self._device.cmd
        readinto = self._device.readinto
        cs = self._device.cs
        cdv = self._device.cdv

        if len(blocks) == 1:
            # Single block read
            if cmd(17, blocks[0].block_num * cdv, 0, release=False) != 0:
                cs(1)
                raise OSError(5)  # EIO
            readinto(blocks[0].content)
        else:
            # Multiblock read
            # CMD18: set read address for multiple blocks
            if cmd(18, blocks[0].block_num * cdv, 0, release=False) != 0:
                # release the card
                cs(1)
                raise OSError(5)  # EIO

            for block in blocks:
                readinto(block.content)

            if cmd(12, 0, 0xFF, skip1=True):
                raise OSError(5)  # EIO

    def write_to_device(self, blocks: list[Block]) -> None:
        """Write blocks fron the device to the cache blocks.
        Uses multiplock operations if possible.
        Write is made from the cache (blocks.content) unless a different buffer is provided
        This is the method which should be changed if the underlaying device changes.
        """
        cmd = self._device.cmd
        write = self._device.write
        spi = self._device.spi
        cdv = self._device.cdv
        write_token = self._device.write_token

        # workaround for shared bus, required for (at least) some Kingston
        # devices, ensure MOSI is high before starting transaction
        spi.write(b"\xff")
        if len(blocks) == 1:
            if cmd(24, blocks[0].block_num * cdv, 0) != 0:
                raise OSError(5)
            write(_TOKEN_DATA, blocks[0].content)
        else:
            # Multiblock write
            if cmd(25, blocks[0].block_num * cdv, 0) != 0:
                raise OSError(5)
            for block in blocks:
                write(_TOKEN_DATA, block.content)

            write_token(_TOKEN_STOP_TRAN)

    def reset_cache(self, cache_max_size: int, policy: str = "LRU", read_ahead: int = 1) -> None:
        """Reset the cache. This is mainly for testing purposes, to change
        the cache size on the fly during test runs."""
        self._cache_max_size = cache_max_size
        self._eviction_policy = policy.upper()
        self._read_ahead = read_ahead

        self._cache: list[bytearray] = [bytearray(self._block_size) for _ in range(cache_max_size)]
        self._blocks: LRMDict = LRMDict()

    def show_cache_status(self):
        """Print the cache status."""
        print("-" * 40)
        print("Cache status")
        # print(" - Block list", self._blocks)
        print(" - Blocks:")
        for num, block in self._blocks.items():
            print(f" -> {num:8d}: {block.block_num:8d} {block.dirty} {list(block.content[:8])}")

    def debug_print(self, *args, **kwargs):
        if self._debug_flags.get("debug_print", False):
            print(*args, **kwargs)


class SDCard:
    def __init__(
        self,
        spi,
        cs,
        baudrate=1320000,
        block_size: int = 512,
        cache_max_size: int = 16,
        eviction_policy: str = "LRUC",
        read_ahead: int = 1,
        **debug_flags,
    ):
        self.spi = spi
        self.cs = cs

        self.cmdbuf = bytearray(6)
        self.dummybuf = bytearray(512)
        self.tokenbuf = bytearray(1)
        for i in range(512):
            self.dummybuf[i] = 0xFF
        self.dummybuf_memoryview = memoryview(self.dummybuf)

        # Temporary buffer for partial block handling
        self._tempbuf = bytearray(512)
        self._mvt = memoryview(self._tempbuf)
        self._cache = Cache(
            self, block_size, cache_max_size, eviction_policy, read_ahead, **debug_flags
        )
        ####
        # Set up Analytics. Can be deleted later. Import or mock class to reduce overhead.
        self._debug_flags = debug_flags
        if self._debug_flags.get("analytics", False):
            from analytics import Analytics  # type: ignore
        else:
            # fmt: off
            print("Analytics not available. To use Analytics.log or Analytics.collect etc. you need to have the analytics.py file in the import path. File avalable inside repo lib forlder. A mock class that does nothing  will be used")  # fmt: skip
            class Analytics:
                def __init__(self, *args, **kwargs):
                    self.fslog: Any
                def collect(self, *args, **kwargs): pass
                def log(self, *args, **kwargs): pass
                def print_all(self): pass
                def print(self, *args, **kwargs): pass
                def print_log(self, *args, **kwargs): pass
                def print_stats(self, *args, **kwargs): pass
                def clear(self, *args, **kwargs): pass
            # fmt: on
        self.a = Analytics(**debug_flags)
        self._cache.a = self.a

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
                self.cmd(58, 0, 0, -4)  # 4-byte response, negative means keep the first byte
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

        # DEBUG
        # if self._cache._debug_flags.get("analytics", False):
        #     # Stats collection
        #     aligned = offset == 0 and (offset + len_buf) % 512 == 0
        #     miss_left = offset > 0 and (offset + len_buf) % 512 == 0
        #     miss_right = offset == 0 and (offset + len_buf) % 512 > 0
        #     miss_both = offset > 0 and (offset + len_buf) % 512 > 0
        #     # self.a.log(f"->sdcard/rb: {block_num}, offset {offset}, nblocks {nblocks}, len_buf {len_buf}")  # fmt: skip
        #     # self.a.collect("sdcard/rb")
        #     if nblocks == 1:
        #         pass
        #         # self.a.collect("sdcard/rb/single")
        #         # self.a.collect("sdcard/rb/single/min", len_buf, mode="min")
        #         # self.a.collect("sdcard/rb/single/max", len_buf, mode="max")
        #         # self.a.collect("sdcard/rb/single/avg", len_buf, mode="avg")
        #         # self.a.collect("sdcard/rb/single/aligned") if aligned else None
        #         # self.a.collect("sdcard/rb/single/miss_left") if miss_left else None
        #         # self.a.collect("sdcard/rb/single/miss_right") if miss_right else None
        #         # self.a.collect("sdcard/rb/single/miss_both") if miss_both else None
        #     else:
        #         pass
        #         # self.a.collect("sdcard/rb/multi")
        #         # self.a.collect("sdcard/rb/multi/min", len_buf, mode="min")
        #         # self.a.collect("sdcard/rb/multi/max", len_buf, mode="max")
        #         # self.a.collect("sdcard/rb/multi/avg", len_buf, mode="avg")
        #         # self.a.collect("sdcard/rb/multi/aligned") if aligned else None
        #         # self.a.collect("sdcard/rb/multi/miss_left") if miss_left else None
        #         # self.a.collect("sdcard/rb/multi/miss_right") if miss_right else None
        #         # self.a.collect("sdcard/rb/multi/miss_both") if miss_both else None

        if nblocks == 1:
            # Only one block to read (partial or complete)
            self._cache.get(block_num, mvt)
            mvb[:] = mvt[offset : offset + len_buf]

        else:
            # More than one block to read
            # CMD18: set read address for multiple blocks

            bytes_read = 0

            # Handle the initial partial block write if there's an offset
            if offset > 0:
                self._cache.get(block_num, mvt)
                bytes_from_first_block = 512 - offset
                mvb[0:bytes_from_first_block] = mvt[offset:]
                bytes_read += bytes_from_first_block
                block_num += 1

            # Read full blocks if any
            while bytes_read + 512 <= len_buf:
                self._cache.get(block_num, mvb[bytes_read : bytes_read + 512])
                bytes_read += 512
                block_num += 1

            # Handle the las partial block if needed
            if bytes_read < len_buf:
                self._cache.get(block_num, mvt)
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

        # DEBUG
        # if self._cache._debug_flags.get("analytics", False):
        #     # Stats collection
        #     aligned = offset == 0 and (offset + len_buf) % 512 == 0
        #     miss_left = offset > 0 and (offset + len_buf) % 512 == 0
        #     miss_right = offset == 0 and (offset + len_buf) % 512 > 0
        #     miss_both = offset > 0 and (offset + len_buf) % 512 > 0
        #     # self.a.log(f"->sdcard/wb: {block_num}, offset {offset}, nblocks {nblocks}, len_buf {len_buf}")  # fmt: skip
        #     # self.a.collect("sdcard/wb")
        #     if nblocks == 1:
        #         pass
        #         # self.a.collect("sdcard/wb/single")
        #         # self.a.collect("sdcard/wb/single/min", len_buf, mode="min")
        #         # self.a.collect("sdcard/wb/single/max", len_buf, mode="max")
        #         # self.a.collect("sdcard/wb/single/avg", len_buf, mode="avg")
        #         # self.a.collect("sdcard/wb/single/aligned") if aligned else None
        #         # self.a.collect("sdcard/wb/single/miss_left") if miss_left else None
        #         # self.a.collect("sdcard/wb/single/miss_right") if miss_right else None
        #         # self.a.collect("sdcard/wb/single/miss_both") if miss_both else None
        #     else:
        #         pass
        #         # self.a.collect("sdcard/wb/multi")
        #         # self.a.collect("sdcard/wb/multi/min", len_buf, mode="min")
        #         # self.a.collect("sdcard/wb/multi/max", len_buf, mode="max")
        #         # self.a.collect("sdcard/wb/multi/avg", len_buf, mode="avg")
        #         # self.a.collect("sdcard/wb/multi/aligned") if aligned else None
        #         # self.a.collect("sdcard/wb/multi/miss_left") if miss_left else None
        #         # self.a.collect("sdcard/wb/multi/miss_right") if miss_right else None
        #         # self.a.collect("sdcard/wb/multi/miss_both") if miss_both else None

        mvt = self._mvt
        mvb = memoryview(buf)

        if nblocks == 1:
            if offset == 0 and (offset + len_buf) == 512:
                # Single complete block, no need to read
                self._cache.put(block_num, mvb)
            else:
                # Single partial block, need to read first
                self._cache.get(block_num, mvt)
                mvt[offset : offset + len_buf] = mvb[:]
                self._cache.put(block_num, mvt)
        else:
            bytes_written = 0
            # Handle the initial partial block write if there's an offset
            if first_misaligned > 0:
                self._cache.get(block_num, mvt)
                bytes_for_first_block = 512 - offset
                mvt[offset:] = mvb[0:bytes_for_first_block]
                self._cache.put(block_num, mvt)
                bytes_written += bytes_for_first_block
                block_num += 1

            # Write full blocks if any
            while bytes_written + 512 <= len_buf:
                self._cache.put(block_num, mvb[bytes_written : bytes_written + 512])
                bytes_written += 512
                block_num += 1

            # Handle the last partial block if needed
            if bytes_written < len_buf:
                self._cache.get(block_num, mvt)
                mvt[0 : len_buf - bytes_written] = mvb[bytes_written:]
                self._cache.put(block_num, mvt)

    def ioctl(self, op, arg):
        if op == 3:  # sync
            # self.a.log(f"->sdcard: ioctl(3) sync")
            # self.a.collect("sdcard/sync/fs")
            self._cache.sync()
            return 0
        if op == 4:  # get number of blocks
            # return 16 * 1024 * 1024 / 512  # Spoofing the number of blocks for testing purposes
            return self.sectors
        if op == 5:  # get block size in bytes
            return 512
        if op == 6:  # Erase block, handled by the controller
            # LFS expects the erased block to be really erased (xff) or it complains about data corruption.
            # This doesn't make a lot of sense in the context of SD cards, but no other option for now.
            block = self._cache._blocks.get(arg, None)
            if block:
                if block.dirty:
                    raise OSError(f"SDCard: ioctl(6,{arg}) - Can't erase a dirty block")
                else:
                    block.content[:] = b"\xff" * 512
                    block.dirty = True
            else:
                self._cache.put(arg, b"\xff" * 512)  # type: ignore
            # self.a.log(f"->sdcard: eraseblock {arg}: {self._cache._blocks}")
            # self.a.collect("sdcard/eraseblock")
            return 0
