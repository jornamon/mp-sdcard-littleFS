"""
A set of tools for analysing the behavior of scripts and debug printing and logging.
Controlled by flags to avoid too much overhead when not needed.

MIT License Jorge Nadal. 2022
"""
import math

try:
    from typing import Sequence
except ImportError:
    pass


class DataStats:
    """DataStats allows you to collect samples anywhere in your code and then calculate some basic stats.
    Valid for any magnitude, but specially useful for analysing durations, latencies, etc.
    The results are returned in a dict with the following keys:
    "samples": number of samples
    "min": min value
    "max": max value
    "avg": mean value
    "stdev": standard deviation
    "range": max - min,
    "overhead": (mean - smin) / smin

    It accepts integers/floats or lists/tuples of int/float."""

    def __init__(self, max_samples: int = 1000, print_result: bool = True):
        self.max_samples = max_samples
        self._samples = []
        self._print_result = print_result

    def add_sample(self, sample: int | float) -> None:
        """Add sample to the collection."""
        if len(self._samples) < self.max_samples:
            self._samples.append(sample)
        else:
            print("\nDataStats: Max samples reached, rejecting additional data")

    def extend_samples(self, samples: Sequence[int | float]) -> None:
        """Add a sequence of samples"""
        if len(self._samples) + len(samples) < self.max_samples:
            self._samples.extend(samples)
        else:
            print("\nDataStats: Max samples reached, rejecting additional data")

    def clear(self):
        """Clears all collected samples."""
        self._samples = []

    def calc_stats(self) -> dict:
        """Return stats from collected data.

        Returns:
            dict: Stats of collected samples
        """
        stats = {}
        num_samples = len(self._samples)

        # Mean
        sums = 0
        for i in range(num_samples):
            sums += self._samples[i]
        mean = sums / num_samples

        # Difference squared
        difference_squared = 0
        for i in range(num_samples):
            difference_squared += (self._samples[i] - mean) ** 2

        stdev = math.sqrt(difference_squared / (num_samples - 1)) if num_samples > 1 else 0
        smax = max(self._samples)
        smin = min(self._samples)

        stats = {
            "samples": num_samples,
            "min": smin,
            "max": smax,
            "avg": mean,
            "stdev": stdev,
            "range": smax - smin,
            "overhead": (mean - smin) / smin,
        }

        if self._print_result:
            for key, value in stats.items():
                print(f"{key:>10}: {value:>8.2f}")
        return stats


class Log:
    """A finite-sized log the stores onlye the last N message. It is
    usefull to log messages and print them only when an error condition is detected, like
    a small stack trace.
    """

    def __init__(self, max_size: int = 50):
        self.max_size = max_size
        self._log = []

    def log(self, message: str) -> None:
        """Add a message to the log."""
        if len(self._log) < self.max_size:
            self._log.append(message)
        else:
            self._log.append(message)
            self._log.pop(0)


class Analytics:
    """Collect stats for events, counters, etc. and store them in a dict.
    Also allows you to print debug messages or log them to print later or under error conditions.
    Modes are all self explainatory but "full" which is collects samples
    and calculates a set of stats for them.

    To activate analytics features you have to pass the corresponding flag as True as kwarg.
    Disabling the features leaves the main code with small overhead.

    collet=True: Collects stats
    aprint=True: Print messages
    log=True: Log messages

    """

    def __init__(self, **flags):
        self.stats = {}
        self.flags = flags
        self.fslog = Log()

    def collect(self, key, value=None, mode="count"):
        if not self.flags.get("analytics", False):
            return
        if key in self.stats:
            if mode == "count":
                self.stats[key] += 1
            elif value is not None:
                if mode == "sum":
                    self.stats[key] += value
                elif mode == "max":
                    self.stats[key] = max(self.stats[key], value)
                elif mode == "min":
                    self.stats[key] = min(self.stats[key], value)
                elif mode == "avg":
                    prev = self.stats[key]
                    self.stats[key] = (prev[0] + value, prev[1] + 1)
                elif mode == "dict":
                    self.stats[key].update(value)
                elif mode == "clear":
                    self.stats[key] = value
                elif mode == "full":
                    self.stats[key].add_sample(value)
        else:
            if mode == "full":
                self.stats[key] = DataStats()
            elif mode == "avg":
                self.stats[key] = (value, 1)
            elif mode == "count":
                self.stats[key] = 1
            else:
                self.stats[key] = value

    def print(self, *args, **kwargs):
        if any(self.flags.get(flag, False) for flag in ["debug_print", "print"]):
            print(*args, **kwargs)

    def log(self, message: str):
        if self.flags.get("analytics", False):
            self.fslog.log(message)

    def print_log(self):
        print("-------- Log --------")
        for message in self.fslog._log:
            print(message)
        print("---------------------")

    def print_stats(self):
        full_list = []
        fclen = max([len(str(key)) for key in self.stats]) + 5
        print("------ Usage Stats ------")
        print(f"{'Key':<{fclen}} {'Value':>10}")
        for key, value in sorted(self.stats.items()):
            if isinstance(value, DataStats):
                # Print at the end
                full_list.append((key, value))
                continue
            elif isinstance(value, tuple):
                # Calculate average
                value = value[0] / value[1]
            elif isinstance(value, dict) or isinstance(value, list):
                value = str(value)
            print(f"{key:<{fclen}} {value:>10}")
        print()
        for key, value in full_list:
            print(f" -> Full stats for {key}:")
            value.calc_stats()
            print()
        print("-----------------------")

    def print_all(self):
        self.print_log()
        self.print_stats()

    def clear(self):
        self.stats = {}
        self.fslog = Log()


if __name__ == "__main__":

    a = Analytics(collect=True, aprint=True, log=True)
    a.fslog.max_size = 5

    for i in range(15):
        a.print(f"Printing {i}")
        a.collect("count")
        a.collect("sum", i, "sum")
        a.collect("max", i, "max")
        a.collect("min", i, "min")
        a.collect("avg", i, "avg")
        a.collect("dict", {"a": i, "b": 2 * i}, "dict")
        a.collect("full", i, "full")
        a.log(f"Logging {i}")
    a.print_log()
    a.print_stats()
    print(a.stats["avg"])
