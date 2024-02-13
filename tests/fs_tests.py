"""
Test Device driver performance for filesystem read and write operations.
TODO: Bug when using read_ahead > 1, only with LFS.
"""
from collections import OrderedDict
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


def mount(sd):
    try:
        os.mount(sd, "/sd")
    except OSError as e:
        pass


def format_fat(sd):
    os.VfsFat.mkfs(sd)
    # print("Formated as FAT")


def format_lfs(sd):
    os.VfsLfs2.mkfs(sd)
    # print("Formated as LFS2")


def write_file(filename: str, mode: str, data: memoryview | bytearray):
    size = len(data)
    if mode == "wb" or mode == "binary":
        mode = "wb"
    elif mode == "w" or mode == "text":
        mode = "w"
    else:
        raise ValueError("Invalid mode")

    start = time.ticks_us()
    with open(filename, mode) as f:
        f.write(data)
    elapsed = time.ticks_diff(time.ticks_us(), start)
    speed = size / 1024 / (elapsed / 1_000_000)
    return elapsed, speed


def read_file(filename: str, mode: str, data: memoryview | bytearray):
    size = len(data)
    if mode == "rb" or mode == "binary":
        mode = "rb"
    elif mode == "r" or mode == "text":
        mode = "r"
    else:
        raise ValueError("Invalid mode")
    size = len(data)
    start = time.ticks_us()
    with open(filename, mode) as f:
        f.readinto(data)  # type: ignore
    elapsed = time.ticks_diff(time.ticks_us(), start)
    speed = size / 1024 / (elapsed / 1_000_000)
    return elapsed, speed


def shuffle(lst):
    for i in range(len(lst) - 1, 0, -1):
        j = random.randint(0, i)
        lst[i], lst[j] = lst[j], lst[i]
    return lst


def test_suite(
    sd,
    filesystem=["LFS"],
    workload=None,
    mode=["binary"],
    cache_size=[4],
    read_ahead=[1],
    eviction_policty=["LRUC"],
):
    """Applies a test suite for a given workload for all possible combinations of the parameters.
    Workload is a list of tuples, each tupple contains the number of files and the size of the file.
    Before starting, the workload is randomized to avoid any bias.
    For Example: [(1, 1 * 1024),(4, 256),...] will create 1 file of 1KB and 4 files of 256B,...
    Calculates the average speed for each combination and store results in a list of tuples."""

    gc.collect()

    # Randomize the workload.
    workload = workload or [
        [2, 64 * 1024],
        [2, 16 * 1024],
        [2, 4 * 1024],
        [2, 1 * 1024],
        [2, 256],
        [2, 64],
    ]

    workload = [x[0] * [x[1]] for x in workload]
    workload = [item for sublist in workload for item in sublist]
    workload = shuffle(workload)

    # Allocate the buffers for read and write operations
    chunk_size = 512
    write_buffer = bytearray(range(chunk_size))
    mvw = memoryview(write_buffer)
    read_buffer = bytearray(chunk_size)
    mvr = memoryview(read_buffer)
    ntests = len(filesystem) * len(cache_size) * len(read_ahead) * len(eviction_policty)
    nfiles = len(workload)
    total_size = sum(workload)
    filesystem = [fs.upper() for fs in filesystem]
    eviction_policty = [e.upper() for e in eviction_policty]
    results = []
    nt = 1
    gc.collect()
    try:
        for fs in filesystem:
            for csize in cache_size:
                for ra in read_ahead:
                    for e in eviction_policty:
                        # Reset cache with new parameters
                        sd._cache.reset_cache(
                            cache_max_size=csize,
                            read_ahead=ra,
                            policy=e,
                        )
                        # Every test starts with a fresh filesystem
                        print(
                            f"Test {nt} of {ntests}. {fs}, cache size {csize}, read ahead {ra} and eviction policy {e}"
                        )
                        if fs == "LFS":
                            format_lfs(sd)
                        elif fs == "FAT":
                            format_fat(sd)
                        else:
                            raise ValueError("Invalid filesystem")
                        mount(sd)
                        gc.collect()

                        # Write the files
                        nf = 1
                        tot_elapsed = 0
                        for size in workload:
                            filename = f"/sd/test_{nt:02d}_{nf:03d}.bin"
                            bytes_left = size
                            start = time.ticks_us()
                            with open(filename, "wb") as f:
                                while bytes_left > 0:
                                    chunk = min(chunk_size, bytes_left)
                                    f.write(mvw[:chunk])  # type: ignore
                                    bytes_left -= chunk
                            elapsed = time.ticks_diff(time.ticks_us(), start)

                            tot_elapsed += elapsed
                            progress = nf * 20 // nfiles
                            print(
                                f"\r Write: {'[' + '#' * progress + '.' * (20 - progress) + ']'}    Read: {'[' + '.' * 20 + ']'}",
                                end="",
                            )
                            nf += 1
                        avg_speed = total_size / 1024 / (tot_elapsed / 1_000_000)
                        results.append((fs, csize, ra, e, "write", avg_speed, tot_elapsed / 1_000_000))  # fmt: skip

                        # Read the files
                        nf = 1
                        tot_elapsed = 0
                        errors = 0
                        for size in workload:
                            filename = f"/sd/test_{nt:02d}_{nf:03d}.bin"
                            bytes_left = size
                            start = time.ticks_us()
                            chunk_error = False
                            with open(filename, "rb") as f:
                                while bytes_left > 0:
                                    chunk = min(chunk_size, bytes_left)
                                    f.readinto(mvr[:chunk])
                                    bytes_left -= chunk
                                    if mvr[:chunk] != mvw[:chunk]:
                                        chunk_error = True
                            elapsed = time.ticks_diff(time.ticks_us(), start)
                            tot_elapsed += elapsed
                            progress = nf * 20 // nfiles
                            print(
                                f"\r Write: {'[' + '#' * 20 + ']'}    Read: {'[' + '#' * progress + '.' * (20 - progress) + ']'}",
                                end="",
                            )
                            nf += 1
                            if chunk_error:
                                # print(f"Error reading file {filename}")
                                errors += 1

                        avg_speed = total_size / 1024 / (tot_elapsed / 1_000_000)
                        results.append((fs, csize, ra, e, "read", avg_speed, tot_elapsed / 1_000_000))  # fmt: skip
                        print()
                        if errors:
                            print(f"Total errors for test {nt}: {errors}")
                        nt += 1
                        os.umount("/sd")
    except Exception as e:
        print(f"\nException in test {nt}, file {nf} '{filename}' of size {size}")
        sd._cache.show_cache_status()
        sd.a.print_all()
        raise e
    finally:
        # Print the results
        print("\nResults")
        print(
            f"{'Filesys':>10} {'Cache':>10} {'RDahead':>10} {'Policy':>10} {'Operation':>10} {'Speed':>8} {'Elapsed':>8}"
        )
        for r in results:
            print(
                f"{r[0]:>10} {r[1]:>10} {r[2]:>10} {r[3]:>10} {r[4]:>10} {r[5]:>8.2f} {r[6]:>8.2f}"
            )


########################

sd = SDCard(
    spi,
    CS,
    cache_max_size=128,
    read_ahead=1,
    eviction_policy="LRUC",
    analytics=True,
    log=True,
    collect=True,
)
sd.a.fslog.max_size = 500

# format_fat(sd)
# mount(sd)
# print(os.listdir("/sd"))
# data_written = os.urandom(64)
# data_read = bytearray(64)
# print(write_file("/sd/test.bin", "wb", data_written))
# print(read_file("/sd/test.bin", "rb", data_read))
# print("Read correct", data_read == data_written)
# print("Data written", data_written)
# print("Data read", data_read)

# workload = [(1, 64 * 1024)]
# workload = [
#     [1, 256 * 1024],
#     [2, 64 * 1024],
#     [4, 16 * 1024],
#     [8, 4 * 1024],
#     [16, 1 * 1024],
#     [32, 256],
#     [64, 64],
# ]
workload = [
    [1, 128 * 1024],
    [2, 64 * 1024],
    [2, 16 * 1024],
    [2, 4 * 1024],
    [2, 1 * 1024],
    [2, 256],
]
test_suite(
    sd,
    workload=workload,
    filesystem=["FAT"],
    cache_size=[32],
    read_ahead=[1,2,4,8,16],
    eviction_policty=["LRUC"],
)

# mount(sd)
# files = os.listdir("/sd")
# print(files)
# prefix = "/sd/"
# for file in files:
#     gc.collect()

#     with open(f"{prefix}{file}", "rb") as f:
#         print(f"Reading file {file}")
#         read_data = bytearray(f.read())
#         if read_data != bytearray(range(len(read_data))):
#             print("Error reading file", file)

sd.a.print_all()
