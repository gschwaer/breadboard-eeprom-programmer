"""
Program EEPROM with provided binary data.

Usage: bbeeprog.py init DEVICE
       bbeeprog.py flash [--only-changes=OLD_FILE] DEVICE FILE
       bbeeprog.py -h | --help

Arguments:
  DEVICE  path to the serial device
  FILE    binary input file

Options:
  -h --help                show this help
  --only-changes=OLD_FILE  flash only bytes that differ in FILE and OLD_FILE

Warning: Make sure to run `bbeeprog.py init` before inserting your EEPROM,
         otherwise the first byte may be overwritten to zero.
"""
from collections.abc import Iterable
import datetime
import itertools
import sys
import time

from docopt import docopt
import serial as pyserial  # type: ignore


class SN74LV8153:
    _serial: pyserial.Serial
    _addr: int
    _last_value: int

    MIN_BAUD_RATE = 2000
    MAX_BAUD_RATE = 24000

    def __init__(self, serial: pyserial.Serial, addr: int):
        assert SN74LV8153.MIN_BAUD_RATE <= serial.baudrate <= SN74LV8153.MAX_BAUD_RATE
        assert 0 <= addr < 8

        self._serial = serial
        self._addr = addr
        self._last_value = -1

    # Protocol layout:
    # Data byte is sent as two nibbles in two consecutive byte sized UART
    # telegrams (8N1, max 24k Baud).
    #
    # 1st Telegram:
    #   ,- regular UART start bit (by UART driver) (=0)
    #   |   ,- protocol start bit (in payload) (=1)
    #   |   |   ,- 3 address bits
    #   |   |   |              ,- 4 low data bits (low nibble)
    #   |   |   |              |                   ,- regular UART stop bit (by
    #   |   |   |              |                   |  UART driver) (=1)
    #   v   v   v              v                   v
    # .----------------------------------------------.
    # | 0 | 1 | A0 | A1 | A2 | D0 | D1 | D2 | D3 | 1 |
    # '----------------------------------------------'
    #     `- - - - - P A Y L O A D  - - - - - - -'
    # Note: UART sends lsb first, so D3 is bit 8.
    #
    # 2nd Telegram:
    # Identical to the first telegram, but contains the high nibble of data.
    # This telegram is required: If the second telegram is not received, the
    # first will be discarded.
    @staticmethod
    def _protocol(addr: int, value: int) -> bytes:
        low_nibble = (value & 0x0F) << 4 | addr << 1 | 1
        high_nibble = (value >> 4) << 4 | addr << 1 | 1

        return bytes([low_nibble, high_nibble])

    def write(self, value: int):
        value = value & 0xFF

        if value == self._last_value:
            # We don't use SOUT and performing this write would not change the
            # chip's output. Omitting the write will speed up EEPROM writing.
            return

        self._serial.write(SN74LV8153._protocol(self._addr, value))
        self._last_value = value


class BbEeProg:
    """Breadboard EEPROM Programmer"""

    _serial_port: str
    _serial: pyserial.Serial
    _addr_lo: SN74LV8153
    _addr_hi: SN74LV8153
    _data: SN74LV8153
    _dt_last_write: datetime.datetime

    # nWE: not Write Enable
    ADDR_HI_WRITE_DISABLE_BIT = 1 << 7
    # USB via UART is not very timing accurate, so we extend the 10ms rating by
    # a bit. On my T400, the smallest deviation I measured was -2.5ms, so double
    # that should be a solid default, I hope.
    WRITE_CYCLE_MS = 10 + 5

    def __init__(self, tty_dev: str):
        self._serial_port = tty_dev

    def __enter__(self):
        self._serial = pyserial.Serial(
            self._serial_port, baudrate=SN74LV8153.MAX_BAUD_RATE
        )

        self._addr_lo = SN74LV8153(self._serial, 0)
        self._addr_hi = SN74LV8153(self._serial, 1)
        self._data = SN74LV8153(self._serial, 2)

        # The SN74LV8153 starts with all output pins low. Write enable is active
        # low, so writing is enabled by default (not good). We initialize the
        # nWE pin to high during init, so the EEPROM should be inserted after
        # initialization, otherwise byte 0 might be overwritten with 0x00.
        self._addr_hi.write(BbEeProg.ADDR_HI_WRITE_DISABLE_BIT)

        # Setting this to `now()`, just in case the EEPROM was inserted before
        # initializing, so a write on byte 0 is in progress (see above). We
        # would get non-deterministic results in this case otherwise and the
        # tiny delay doesn't hurt on the happy path.
        self._dt_last_write = datetime.datetime.now()

        return self

    def __exit__(self, t, v, b):
        self._serial.close()

    def write_byte(self, addr: int, byte: int):
        assert addr <= 0x7FFF

        # There are some timing requirements in the <= 100 ns range for byte
        # writes on the AT28C256. They are all lower limits. We always satisfy
        # them, because the maximum baud rate slows us down into the micro
        # second range.

        self._data.write(byte)
        self._addr_lo.write(addr & 0xFF)
        # According to the AT28C256 data sheet, the address setup time before
        # nWE goes low is >= 0 ns. However, it also states that the address is
        # latched on the falling edge of nWE. If we would set up the address
        # lines together with pulling nWE low (below), capacitive signal delays
        # might ruin the write, so we set the address lines up in advance.
        self._addr_hi.write(addr >> 8 | BbEeProg.ADDR_HI_WRITE_DISABLE_BIT)

        delay = (
            (
                self._dt_last_write
                + datetime.timedelta(milliseconds=BbEeProg.WRITE_CYCLE_MS)
            )
            - datetime.datetime.now()
        ).total_seconds()
        if delay > 0:
            time.sleep(delay)

        # strobe write enable (nWE, active low)
        self._addr_hi.write(addr >> 8)
        self._addr_hi.write(addr >> 8 | BbEeProg.ADDR_HI_WRITE_DISABLE_BIT)
        self._dt_last_write = datetime.datetime.now()

    def write(self, data: Iterable[int], old_data: Iterable[int] = []):
        last_chunk_was_old = False
        last_chunk_start = 0
        for addr, (byte, old_byte) in enumerate(itertools.zip_longest(data, old_data)):
            assert addr < pow(2, 15), "Address overflow, too much data"

            if byte is None:  # old_data is larger than data
                break

            if byte == old_byte:
                if not last_chunk_was_old:
                    # start of old chunk
                    last_chunk_was_old = True
                    if addr != 0:
                        print(
                            f"{last_chunk_start:04x}...{addr - 1:04x}: Wrote "
                            f"{addr - last_chunk_start} byte"
                        )

                    last_chunk_start = addr

                continue
            else:
                if last_chunk_was_old:
                    # end of old chunk
                    last_chunk_was_old = False
                    print(
                        f"{last_chunk_start:04x}...{addr - 1:04x}: Skipped "
                        f"{addr - last_chunk_start} byte (no change)"
                    )
                    last_chunk_start = addr

                self.write_byte(addr, byte)

        if not last_chunk_was_old:
            print(
                f"{last_chunk_start:04x}...{addr:04x}: Wrote "
                f"{addr + 1 - last_chunk_start} byte"
            )
        else:
            if last_chunk_start == 0:
                print("Nothing to do (no changes)")
            else:
                print(
                    f"{last_chunk_start:04x}...{addr:04x}: Skipped "
                    f"{addr + 1 - last_chunk_start} byte (no change)"
                )

    def _file_read_bytes(self, file: str) -> Iterable[int]:
        with open(file, "r+b") as f:
            while True:
                data = f.read(1)
                if data == b"":
                    break

                yield data[0]

    def write_file(self, file: str):
        self.write(self._file_read_bytes(file))

    def write_file_diff(self, file: str, diff_file: str):
        self.write(self._file_read_bytes(file), self._file_read_bytes(diff_file))


if __name__ == "__main__":
    arguments = docopt(__doc__)

    with BbEeProg(arguments["DEVICE"]) as bbeeprog:
        if arguments["init"]:
            # programmer was initialized in EEProg.__init__()
            print("Initialized. Ready for EEPROM insertion.")
            sys.exit(0)

        if arguments["flash"]:
            ts_start = time.time()
            if arguments["--only-changes"]:
                print("Note: Flashing in changes only mode.")
                bbeeprog.write_file_diff(arguments["FILE"], arguments["--only-changes"])
            else:
                bbeeprog.write_file(arguments["FILE"])

            ts_end = time.time()
            print(f"Writing took {round(ts_end-ts_start, 3)}s.")
