"""
Tests for the new sdcard driver that implements de block device extended interface.
"""

import os
import random
import time
import gc
from machine import Pin, SPI
from sdcard_lfs import SDCard

# from sdcard import SDCard

SCK = Pin(36)
MOSI = Pin(35)
MISO = Pin(37)
SPI_N = const(2)
CS = Pin(14, Pin.OUT, value=1)
spi = SPI(SPI_N, baudrate=25_000_000, sck=SCK, mosi=MOSI, miso=MISO)


sd = SDCard(spi, CS, debug=True, cache_max_size=256)
# sd = SDCard(spi, CS)  # Use this for the original driver


def mount(sd):
    try:
        os.mount(sd, "/sd")
    except OSError as e:
        print("probably already mounted")
        print(e)
    print(os.listdir("/sd"))


def format_fat(sd):
    os.VfsFat.mkfs(sd)
    print("Formated as FAT")
    # os.mount(sd, "/sd")
    # print(os.listdir("/"))


def format_lfs(sd):
    os.VfsLfs2.mkfs(sd)
    print("Formated as LFS2")
    # os.mount(sd, "/sd")
    # print(os.listdir("/"))


def block_rw_test(sd, blocknum, length=512, offset=0, do_print=True):
    """Test read and right consistency for a call to readblocks and writeblocks."""
    if do_print:
        print("-" * 40)
        print("RW TEST:")
        print(f" - blocknum {blocknum}")
        print(f" - length {length}")
        print(f" - offset {offset}")
    buf_read = bytearray(length)
    buf_write = bytearray(os.urandom(length))
    sd.writeblocks(blocknum, buf_write, offset)
    sd.readblocks(blocknum, buf_read, offset)
    if do_print:
        if buf_read == buf_write:
            print("Result: PASSED")
        else:
            print("Result: FAILED")
        print("-" * 40)
    # time.sleep_ms(5)
    if buf_read != buf_write:
        print("-" * 40)
        print("RW TEST FAILED:")
        print(f" - blocknum {blocknum}")
        print(f" - length {length}")
        print(f" - offset {offset}")
        print("offset + length", offset + length)
        print("Aligned", offset == 0 and (offset + length) % 512 == 0)
        print("Miss left", offset > 0 and (offset + length) % 512 == 0)
        print("Miss right", offset == 0 and (offset + length) % 512 > 0)
        print("Miss both", offset > 0 and (offset + length) % 512 > 0)
        print("buf_read", buf_read)
        print("buf_write", buf_write)
        print("sd.read_cache", sd.read_cache)
        print("sd.read_cached_block", sd.read_cached_block)
    return 0 if buf_read == buf_write else 1


def linear_random_test(sd, block_start=250_000, block_end=750_000, N=10):
    """Run N random tests. Any length, any offset.
    Writes, reads test one by one"""
    fail = success = 0
    for i in range(N):
        blocknum = random.randint(block_start, block_end)
        length = random.randint(8, 8192)
        offset = random.randint(0, 4096)
        if block_rw_test(sd, blocknum, length, offset, do_print=False):
            fail += 1
        else:
            success += 1
    print("Random test results")
    print(f" - Success {success} ({success / N * 100}%)")
    print(f" - Fail {fail} ({fail / N * 100}%)")


def circular_random_test(sd, block_start=250_000, block_end=750_000, N=10):
    """Run N random tests. Any length, any offset.
    Writes all, then test all"""
    fail = success = 0
    write_bufs = []
    lengths = []
    block_nums = []
    offsets = []
    test_buf = bytearray(512)
    for i in range(N):
        block_nums.append(blocknum := random.randint(block_start, block_end))
        offsets.append(offset := random.randint(0, 64))
        lengths.append(length := random.randint(8, 64))
        data = bytearray(os.urandom(length))
        write_bufs.append(data)
        sd.writeblocks(blocknum, data, offset)
        sd.rbdevice(blocknum, test_buf)
        if test_buf[offset : offset + length] != data:
            print("immediate test failed for block", blocknum)
            print(" - offset", offset)
            print(" - length", length)
            print(" - data", data)
            print(" - test_buf[offset : offset + length]", test_buf[offset : offset + length])  # fmt: skip

    # print("Write done")
    # print("write_bufs", write_bufs)
    # print("block_nums", block_nums)
    # print("offsets", offsets)
    # print("lengths", lengths)

    sd.sync()
    # Now read and verify all
    debug_buf = bytearray(512)
    for i in range(N):
        read_buf = bytearray(lengths[i])
        sd.readblocks(block_nums[i], read_buf, offsets[i])
        # sd.rbdevice(block_nums[i], read_buf, offsets[i])
        if read_buf != write_bufs[i]:
            fail += 1
            print("-" * 40)
            print(f"Failed Circular test {i}")
            print(f" - blocknum {block_nums[i]}")
            print(f" - length {lengths[i]}")
            print(f" - offset {offsets[i]}")
            print(" - read_buf\n", read_buf)
            print(" - write_bufs[i]\n", write_bufs[i])
            print(" - cache_block", sd.cache_block)
            print(" - cache content \n", sd.cache[offsets[i] : offsets[i] + lengths[i]])
            sd.rbdevice(block_nums[i], debug_buf)
            print(" - Read from the device")
            print("   - Read buf\n", debug_buf[offsets[i] : offsets[i] + lengths[i]])
            break
        else:
            success += 1
    print("Circular Random test results")
    print(f" - Success {success} ({success / N * 100}%)")
    print(f" - Fail {fail} ({fail / N * 100}%)")


def aligned_test(sd, block_start, block_end, N):
    """Run N tests with aligned block(s)."""
    fail = success = 0
    for i in range(N):
        blocknum = random.randint(block_start, block_end)
        length = 512 * random.randint(1, 8)
        offset = 0
        if block_rw_test(sd, blocknum, length, offset, do_print=False):
            fail += 1
        else:
            success += 1
    print("Random test results")
    print(f" - Success {success} ({success / N * 100}%)")
    print(f" - Fail {fail} ({fail / N * 100}%)")


def raw_read_speed(sd, block_start, block_end, N):
    """Measure read speed in KB/s. No File System, contiguous blocks."""
    start = time.ticks_us()
    blocknum = random.randint(block_start, block_end)
    sd.readblocks(blocknum, bytearray(512 * N))
    elapsed = time.ticks_diff(time.ticks_us(), start)
    speed = 512 * N / 1024 / (elapsed / 1_000_000)
    print(f"Read speed: {speed} KB/s")
    return speed


def raw_write_speed(sd, block_start, block_end, N):
    """Measure write speed in KB/s. No File System, contiguous blocks."""
    start = time.ticks_us()
    blocknum = random.randint(block_start, block_end)
    sd.writeblocks(blocknum, os.urandom(512 * N))
    elapsed = time.ticks_diff(time.ticks_us(), start)
    speed = 512 * N / 1024 / (elapsed / 1_000_000)
    print(f"Write speed: {speed} KB/s")
    return speed


def file_rw_speed(sd, file_name, mode, size):
    """Writes and read file, measures speed in KB/s."""

    def random_string(length):
        chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        return "".join(chars[random.getrandbits(6) % len(chars)] for _ in range(length))

    if mode == "binary":
        data = os.urandom(size)
        wm = "wb"
        rm = "rb"
    else:
        data = random_string(size)
        wm = "wt"
        rm = "rt"

    # Writing
    stats_clear(sd)
    start = time.ticks_us()
    with open(file_name, wm) as f:
        f.write(data)
    elapsed = time.ticks_diff(time.ticks_us(), start)
    speed = size / 1024 / (elapsed / 1_000_000)
    print(f"Write {mode} speed: {speed} KB/s")
    print("Stats after writing")
    stats_print(sd)

    # Reading
    stats_clear(sd)
    start = time.ticks_us()
    with open(file_name, rm) as f:
        data_read = f.read()
    elapsed = time.ticks_diff(time.ticks_us(), start)
    speed = size / 1024 / (elapsed / 1_000_000)
    print(f"Read {mode} speed: {speed} KB/s")

    if data == data_read:
        print("OK reading: data matches written data")
    else:
        print("ERROR reading: data does not match written data")
    print("Stats after reading")
    stats_print(sd)


def read_folder(sd, folder_path):
    """Read a folder and print the first 10 bytes of each file."""
    # stats_clear(sd)
    print("Folder read test")
    for file in os.listdir(folder_path):
        file_path = folder_path + "/" + file
        if os.stat(file_path)[0] & 0x8000:  # Check if it's a file
            with open(file_path, "rb") as f:
                content = f.read()
                print(content[:10])
    # print("Stats after reading files")
    # stats_print(sd)


def stats_clear(sd):
    if hasattr(sd, "stats"):
        sd.stats.clear()


def stats_print(sd):
    if hasattr(sd, "stats"):
        sd.stats.print_stats()


# Test with raw blocks
stats_clear(sd)
# blocknum = 479782
# data = bytearray(range(512))
# readbuf = bytearray(16)
# sd.writeblocks(blocknum, data)
# for i in range(32):
#     sd.readblocks(blocknum, readbuf, offset=i * 16)
#     if readbuf != data[i * 16 : (i + 1) * 16]:
#         print("Failed reading")
# block_rw_test(sd, 445987, 512, 0)
# aligned_test(sd, 500_000, 800_000, 50)
# raw_read_speed(sd, 500_000, 800_000, 100)
# raw_write_speed(sd, 500_000, 800_000, 100)
# print("Linear random test")
# linear_random_test(sd, bs := random.randint(250_000, 750_000), bs + 5, 100)
# print("Circular random test")
# circular_random_test(sd, bs := random.randint(100_000, 150_000), bs + 50000, 50)
# stats_print(sd)
# print(sd.cache_block)
# print(sd.cache)

# # Test with file system
# stats_clear(sd)
format_lfs(sd)
# format_fat(sd)
# print("Usage stats after formating")
# stats_print(sd)

# # stats_clear(sd)
mount(sd)
# # print("Usage stats after mounting")
# # stats_print(sd)

# # Read folder
# # os.mkdir("/sd/test_folder")
# print("/sd content")
# read_folder(sd, "/sd")
# # stats_clear(sd)
file_rw_speed(sd, "/sd/st.bin", "binary", 1024 * 64)
# # # file_rw_speed(sd, "/sd/speed_test.txt", "text", 1024 * 512)
# # print("Usage stats after file rw speed test")
# stats_print(sd)
sd.show_cache_status()
