# Introduction

This is a Micropython SD card driver that works with LittleFS2 (implements extended interface).

This driver builds on top of the original micropython-lib driver [sdcard.py](https://github.com/micropython/micropython-lib/blob/master/micropython/drivers/storage/sdcard/sdcard.py).

The original driver is a block device driver that works with FAT filesystems. This driver extends the original driver, implementing the block device extended interface, which allow for arbitrary offsets and lengths whe calling readblocks and writeblocks. You can learn more about simple and extended interface [here](https://docs.micropython.org/en/latest/library/os.html#simple-and-extended-interface)

This allows the driver to be used with LittleFS2, a filesystem that is not block aligned. Since the driver can handle arbitrary offset/length reads and writes, it can also be used with FAT filesystems and any other filesystem that relies on the block device simple or extended interface.

I wanted to try LFS on an SD card because it's a more modern filesystem than FAT, and while it's apparently tailored for smaller devices, I wanted to take advantage of the more modern features and better reliability regarding data corruption, while taking advantage of the cheaper and larger storage capacity of an SD card. I'm interesting in data logging in micropython anyways, so I wanted to try this out.

Reading LFS from a computer is possible but not trivial, so if you plan to read the card with a computer, you might want to stick with FAT. Nonetheless, I find myself many times reading the files through micropython/mpremote directly or not reading them at all from the computer. A nice use case are these XTSD "integrated" SD card chips, that function as SDcards but don't have a card at all, and cannot be read from the computer. Like [these](https://www.adafruit.com/product/4899) sold by Adafruit, which offer super cheap large storage.

I soon discovered that LittleFS is not a very good match for SD cards out of the box.

FAT reads always full 512-byte aligned blocks and writes either a full 512-byte aligned or 4 KB (8 blocks) also aligned.

I guess it may be configurable, but the stock version that comes with micropython of LittleFS makes many missaligned small reads and writes, and no multiblock request whatsoever. This is a problem because the SD card driver reads and writes in 512-byte blocks, and it's not very efficient to read and write 512 bytes when you only need 1 byte. Making LFS perform poorly in comparison to FAT.

`sdcard_ext.py` is the most basic implementation of the block device extended interface I could come up with, but doesn't perform very well with LittleFS. I leave it there, just in case.

`sdcard_lfs.py` implements a simple yet configurable block cache that allows to use LittleFS with more decent performance.

Without a cache, there are when using LFS:

- Small reads and writes hit the same block again and again, forcing you the fetch the same block from the device multiple times.
- You will only be using single block reads and writes. SD Cards con perform more efficient operations on contiguous blocks, but since the filesystem is not aware, the driver must implement some logic to take advantage od this.

Even though FAT seem a little bit better naturally suited for SD cards, you can still improve performance using a driver with a cache like this one and tweaking the parameters.

If you're interested in experimenting with different cache configurations, consider this trade-offs:

- Caches can **improve performance, but not for free**.
- The larger the cache, the **more memory** they consume.
- The larger the cache, the more potential data out of sync with the device, which increments the risk of **data loss** or corruption in case of power failure.
- Even though you can enjoy better overall performance, sync or flush operations can be slower with larger caches, making those big **spikes in latency** even worse. On the bright side, under certain situations you may control when to sync the filesystem to enjoy the better performance when needed and sync whe the time is appropriate. just be aware that the filesystem issues their own sync commands, so you may not be able to control everything.
- The most important thing: there is **no single best cache configuration**. It depends on the hardware you're using, tha application and specially on the type of load you're putting on the filesystem. It's vastly different to have few big reads, many small writes, or a mixed workload.

So the bad news is that you have to find the best configuration for your specific use case. The good news is that you can do it with this driver and it's easy and fun.

The default configuration it's what I found to be a good balance for mixed workloads, without too much memory hit.

## Driver / Cache features

### Cache size

`cache_max_size` is the number of **blocks** that the cache can hold. The cache is a list of blocks that are read from the device and kept in memory. Blocks, not bytes or kilobytes. 

Normally, the larger the cache, the better the performance, but with all the trade-offs mentioned before.

### Read ahead

`read_ahead` is the number of **blocks** that the driver will read from the device when a read request is made. This is a form of prefetching, and it's useful when you're reading sequentially from the device. This is a common pattern when reading files and takes advantage of the multiblock read capabilities od the SD cards, but there's still some guesswork involved because you may be reading blocks that will never be requested, so the performance improvements can stall or even decrease with larger read ahead values. Of course, it depends on the workload.

Minimum value is 1 (no read ahead), and the maximum value is the cache size. But that upper limit is the hard limit, going above half the cache size might be counterproductive for most workloads. Super long reads might benefit the most from this feature.

### Eviction policy


### Analytics

## Usage


