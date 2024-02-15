# Introduction

This is a Micropython SD card driver that works with LittleFS2 (implements extended interface).

This driver builds on top of the original micropython-lib driver [sdcard.py](https://github.com/micropython/micropython-lib/blob/master/micropython/drivers/storage/sdcard/sdcard.py).

The original driver is a basic-interface block device driver that works with FAT filesystems. This driver extends the original driver, implementing the block device extended interface, which allow for arbitrary offsets and lengths when calling readblocks and writeblocks. You can learn more about simple and extended interface [here](https://docs.micropython.org/en/latest/library/os.html#simple-and-extended-interface)

This allows the driver to be used with LittleFS2, a filesystem that is not block aligned. Since the driver can handle arbitrary offset/length reads and writes, it can also be used with FAT filesystems and any other filesystem that relies on the block device simple or extended interface.

I wanted to try LFS on an SD card because it's a more modern filesystem than FAT, and while it's apparently tailored for smaller devices, I wanted to leverage the more modern features and better reliability regarding data corruption, while taking advantage of the cheaper and larger storage capacity of an SD card. I'm interesting in data logging in micropython anyways, so I wanted to try this out.

Reading LFS from a computer is possible but not trivial, so if you plan to read the card with a computer, you might want to stick with FAT. Nonetheless, I find myself many times reading the files through micropython/mpremote directly or not reading them at all from the computer. A nice use case are these XTSD "integrated" SD card chips, that function as SDcards but don't have a card at all, and cannot be read from the computer. Like [these](https://www.adafruit.com/product/4899) sold by Adafruit, which offer super cheap large storage.

I soon discovered that LittleFS is not a very good match for SD cards out of the box. In fact, if you seek raw performance, FAT is a better choice for SD cards. The reason is that FAT is block aligned, like the SD card itself. LittleFS is not, it seems to be designed for block devices that can be addressed at the byte level, like Flash chips.

FAT reads always full 512-byte aligned single blocks and writes either a full 512-byte aligned or 4 KB (8 blocks) also aligned. If you stick to FAT, you can squeeze a little bit more performance by using a driver with a cache, like this one, but the performance is already pretty good.

I guess it may be configurable, but the stock version that comes with micropython of LittleFS makes many missaligned small reads and writes, and no multiblock request whatsoever. This is a problem because the SD card driver reads and writes in 512-byte blocks, and it's not very efficient to read and write 512 bytes when you only need a few byte. Making LFS perform poorly in comparison to FAT. With the cache, you can mitigate and barely hit original driver performance level under certain circumstances, but head to head with the exact same conditions, FAT is faster.

If you want to take a look at performance test results, you can check [**TEST_RESULTS.md**](TEST_RESULTS.md).

`sdcard_ext.py` is the most basic implementation of the block device extended interface I could come up with, but doesn't perform very well with LittleFS. I leave it there, just in case.

`sdcard_lfs.py` implements a simple yet configurable **block cache** that allows to use LittleFS with more decent performance (or increase FAT performance).

Without a cache, there are some limitations when using LFS:

- Small reads and writes hit the same block again and again, forcing you the fetch the same block from the device multiple times.
- You will only be using single block reads and writes. SD Cards can perform more efficient operations on contiguous blocks, but since the filesystem is not aware, the driver must implement some logic to take advantage of this.

Even though FAT seem a little bit better naturally suited for SD cards, you can still improve performance using a driver with a cache like this one and tweaking the parameters.

If you're interested in experimenting with different cache configurations, consider these trade-offs:

- Caches can **improve performance, but not for free**.
- The algorithms that manage the cache consume MCU cycles, introducing an **overhead**. For powerfull MCUs and/or slow IO, the trade will be beneficial, but for slow MCUs and/or fast IO, the overhead may harm overall performance.
- The larger the cache, the **more memory** they consume.
- The larger the cache, the more potential data out of sync with the device, which increments the risk of **data loss** or corruption in case of power failure.
- Even though you can enjoy better overall performance, sync or flush operations can be slower with larger caches, making those big **spikes in latency** even worse. On the bright side, under certain situations you may control when to sync the filesystem to enjoy the better performance when needed and sync whe the time is appropriate. Just be aware that the filesystem issues its own sync commands, so you may not be able to control everything.
- The most important thing: there is **no single best cache configuration**. It depends on the hardware you're using, the application and specially on the type of load you're putting on the filesystem. It's vastly different to have few big reads, many small writes, or a mixed workload.

So the bad news is that you have to find the best configuration for your specific use case. The good news is that you can do it with this driver and it's easy and fun.

The default configuration it's what I found to be a good balance for mixed workloads, without too much memory hit.

## Driver / Cache features

### Cache size

`cache_max_size` is the number of **blocks** that the cache can hold. The cache is a list of blocks that are read from the device and kept in memory. Blocks, not bytes or kilobytes.

Normally, the larger the cache, the better the performance (up to a point), but with all the trade-offs mentioned before. See [**TEST_RESULTS.md**](TEST_RESULTS.md) for some performance tests with different cache sizes for my testing conditions.

### Read ahead

`read_ahead` is the number of **blocks** that the driver will read from the device when a read request is made. This is a form of prefetching, and it's useful when you're reading sequentially from the device. This is a common pattern when reading files and takes advantage of the multiblock read capabilities od the SD cards, but there's still some guesswork involved because you may be reading blocks that will never be requested, so the performance improvements can stall or even decrease with larger read ahead values. Of course, it depends on the workload.

Minimum and default value is 1 (no read ahead), and the maximum value is half the cache size. Technically it could be more, but this feature can start impacting performance negatively very fast. The gains are small and only for certain conditions. See [**TEST_RESULTS.md**](TEST_RESULTS.md) for some examples.

The performance gains under the workloads I tested are minimal (if not negative), and this feature forced me to introduce additional code for handling LFS block erases, so I'm not sure if it's worth it. I'm leaving it there for now, but I'm not sure if I would recommend using it, specially with LFS.

### Eviction policy

The `eviction_policy` is the algorithm that decides which block to remove from the cache when it's full and a new block needs to be added. Currently there are two options: LRU (Least Recently Used block) and LRUC (Least Recently Used and Clean block). Default is LRUC, which apparently performs a bit better in my [tests](TEST_RESULTS.md).

If you feel creative, you can implement your own eviction policy. It's just a function that takes the number of blocks to be evicted and returns a list of blocks to be evicted.

## Usage

The usage is very similar to the original driver. If you use don't set any cache parameters, you can instantiate it in the exact same way:

```python
import machine, sdcard_lfs, os
    sd = sdcard_lfs.SDCard(
        machine.SPI(1),
        machine.Pin(15),
    )
```

Or set the cache parameters:

```python
import machine, sdcard_lfs, os
    sd = sdcard_lfs.SDCard(
        machine.SPI(1),
        machine.Pin(15),
        cache_max_size=16,
        read_ahead=1,
        eviction_policy="LRUC",
    )
```

Then, you can use it as a block device. Don't forget to create the filesystem if it's not already created and mount it:

```python
os.VfsLfs2.mkfs(sd)   # If not already formated and want to use LFS **OR** 
os.VfsFat.mkfs(sd)    # If not already formated and want to use FAT
os.mount(sd, '/sd')   # If not already mounted
os.listdir('/')
```

## Testing

The best way to test is to time the operations you're interested in, but there is handy script that runs a series of tests and prints the results in [tests/fs_test.py](tests/fs_test.py). This is the script I used for the tests in [**TEST_RESULTS.md**](TEST_RESULTS.md).

The workload is a list of lists containing the number of files and the size of the files to be written, read and checked for consistency. The script tests all possible combinations of cache parameters.

Take a look inside if you're interested in the details.

## Analytics

While developing this driver, I added some analytics to help me understand the performance and the behavior of the cache and the requests made from the filesystem.

Some block of this analytics code are activated by including the flag `analytics=True` when instantiating the driver.

It collects information about almost every event that happens in the driver: readblocks and writeblocks requests (size, number, aligned, missaligned, etc), cache hits and misses, cache evictions, ioctl commands and so on. This collection feature is activated by passing the flag `collect=True` when instantiating the driver.

There is also another flag `log=True` that records logs of the events (up to a certain size) and can be printed at the end, at interesting points or under exceptions to help debug.

With this features activated, the driver gets pretty slow. It's just for debugging and understanding the behavior of the driver. Without the flags, the driver is faster because returns immediately from this debug calls, but it's still slower than with everythong commented out, so don't test performance with this code active, even withut the flags.

Commeting everything out leaves the code cluttered, so I might as well remove everything from the main file and leave a version with the analytics code in for testing and debugging.

Since this is a feature more for testing and debugging, I'm not going to explain how to use it here. If you're interested, take a look at the code.

Just some examples of the kind of information you can get:

```output
------ Usage Stats ------
Key                                      Value
cache/get/hit                              290
cache/get/miss                              22
cache/get/miss/full                         19
cache/get/miss/full/ra_avoided               2
cache/get/miss/not_full                      3
cache/put                                  131
cache/put/hit                              131
cache/sync/total                            11
sdcard/eraseblock                           35
sdcard/rb/single/miss_both                  74
sdcard/rb/single/miss_left                  32
sdcard/rb/single/miss_right                 75
sdcard/sync/fs                               3
sdcard/wb                                  131
sdcard/wb/single                           131
sdcard/wb/single/avg                   126.534
sdcard/wb/single/max                       128
sdcard/wb/single/min                        64
sdcard/wb/single/miss_both                  65
sdcard/wb/single/miss_left                  32
sdcard/wb/single/miss_right                 34
-----------------------

----------------------------------------
Cache status
 - Blocks:
 ->   643777:   643777 False [142, 22, 250, 40, 66, 139, 180, 83]
 ->   643778:   643778 False [151, 198, 204, 221, 211, 16, 172, 98]
 ->   643780:   643780 False [224, 150, 240, 239, 30, 36, 8, 106]
 ->   643779:   643779 False [51, 144, 143, 173, 26, 101, 135, 250]
 ->   643781:   643781 True [73, 78, 186, 143, 242, 126, 182, 181]
 ->   643782:   643782 True [248, 83, 122, 206, 217, 13, 208, 14]
 ->   643784:   643784 True [6, 2, 76, 50, 56, 89, 255, 212]
 ->   643783:   643783 True [123, 152, 49, 138, 102, 102, 223, 241]

------ RingLog ------
->sdcard: eraseblock 433496: OrderedDict({433493: Block(433493, True), 433492: Block(433492, True), 433494: Block(433494, True), 433495: Block(433495, True)})
->cache/get/hit 433495
->sdcard/wb: 433496, offset 0, nblocks 1, len_buf 128
->cache/get/miss/full 433496
->block_evictor(2) LRUC, not enough clean blocks, syncing
->cache/sync dirty blocks [Block(433492, True), Block(433493, True), Block(433494, True), Block(433495, True)]
->cache/sync dirty block groups [[Block(433492, False), Block(433493, False), Block(433494, False), Block(433495, False)]], blocks OrderedDict({433493: Block(433493, False), 433492: Block(433492, False), 433494: Block(433494, False), 433495: Block(433495, False)})
->block_evictor(2) LRUC, returned [Block(433493, False), Block(433492, False)]
->cache/get/miss/full evicted blocks before processing [Block(433493, False), Block(433492, False)]
->cache/get/miss/full evicted blocks after processing [Block(433496, False), Block(433497, False)]
->cache/get/miss/full cache blocks before reading from device OrderedDict({433494: Block(433494, False), 433495: Block(433495, False), 433496: Block(433496, False), 433497: Block(433497, False)})
->cache/get/miss/full cache blocks after reading from device OrderedDict({433494: Block(433494, False), 433495: Block(433495, False), 433496: Block(433496, False), 433497: Block(433497, False)})
->cache/put/hit block num 433496
->cache/get/hit 433496
->sdcard/wb: 433496, offset 128, nblocks 1, len_buf 128
->cache/get/hit 433496
->cache/put/hit block num 433496
->cache/get/hit 433496
->sdcard/wb: 433496, offset 256, nblocks 1, len_buf 128
->cache/get/hit 433496
->cache/put/hit block num 433496
->cache/get/hit 433496
->sdcard/wb: 433496, offset 384, nblocks 1, len_buf 128
->cache/get/hit 433496
->cache/put/hit block num 433496
->cache/get/hit 433496
->sdcard: eraseblock 433497: OrderedDict({433494: Block(433494, False), 433495: Block(433495, False), 433497: Block(433497, False), 433496: Block(433496, True)})
->sdcard/wb: 433497, offset 0, nblocks 1, len_buf 128
->cache/get/hit 433497
->cache/put/hit block num 433497
->cache/get/hit 433497
->sdcard/wb: 433497, offset 128, nblocks 1, len_buf 128
->cache/get/hit 433497
->cache/put/hit block num 433497
->cache/get/hit 433497
->sdcard/wb: 433497, offset 256, nblocks 1, len_buf 128
->cache/get/hit 433497
->cache/put/hit block num 433497
->cache/get/hit 433497
->sdcard/wb: 433497, offset 384, nblocks 1, len_buf 128
->cache/get/hit 433497
->cache/put/hit block num 433497
->cache/get/hit 433497
->sdcard: eraseblock 433498: OrderedDict({433494: Block(433494, False), 433495: Block(433495, False), 433496: Block(433496, True), 433497: Block(433497, True)})
->cache/get/hit 433497
->cache/get/hit 433496
->cache/get/hit 433494