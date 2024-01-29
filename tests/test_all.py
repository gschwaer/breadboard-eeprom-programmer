import pytest

from bbeeprog import SN74LV8153, BbEeProg
import serial as pyserial  # type: ignore


def test_protocol():
    buffer = SN74LV8153._protocol(5, 0xAB)
    assert len(buffer) == 2
    assert buffer[0] == 0xBB
    assert buffer[1] == 0xAB


@pytest.mark.skip(reason="manual")
def test_hardware():
    """Output test without EEPROM

    Using a signal analyzer we can check if we wired everything correctly.

    This test should put all values from 0-255 onto all chips' outputs.

    Note: Assumes the device is detected as `/dev/ttyUSB0`.
    """
    serial = pyserial.Serial("/dev/ttyUSB0", baudrate=24000)
    addr_lo = SN74LV8153(serial, 0)
    addr_hi = SN74LV8153(serial, 1)
    data = SN74LV8153(serial, 2)

    for n in range(256):
        addr_lo.write(n)
        addr_hi.write(n)
        data.write(n)


@pytest.mark.skip(reason="manual")
def test_eeprom_content_write():
    """Fill the EEPROM with known data

    Write 32k with byte values from 0 to 255 (repeating), but clamp the lowest
    number to the number of completed repetitions, e.g.:
    ```
    0,1,2,3, ... 255  # byte   0 to 255
    1,1,2,3, ... 255  # byte 256 to 511
    2,1,2,3, ... 255  # byte 512 to 767
    ```
    and so forth.

    Read back with test below.

    Note: Assumes the device is detected as `/dev/ttyUSB0`.
    """
    bbeeprog = BbEeProg("/dev/ttyUSB0")
    values = (max(n % 256, int(n / 256)) for n in range(pow(2, 15)))
    bbeeprog.write(values)


@pytest.mark.skip(reason="manual")
def test_eeprom_content_read():
    """Read back data (with signal analyzer)

    Read 32k bytes from EEPROM. We cannot really read anything using the
    SN74LV8153, so we have to sample the data lines with a signal analyzer and
    manually analyze the result.

    It's easiest to connect the signal analyzer to the pins of the SN74LV8153.

    Using a signal analyzer we can read the content: Pull the nOE of the data
    SN74LV8153 high (disable) and the nOE of the EEPROM low (enable).

    Note: Assumes the device is detected as `/dev/ttyUSB0`.
    """
    serial = pyserial.Serial("/dev/ttyUSB0", baudrate=24000)
    addr_lo = SN74LV8153(serial, 0)
    addr_hi = SN74LV8153(serial, 1)

    for addr in range(pow(2, 15)):
        addr_lo.write(addr & 0xFF)
        addr_hi.write(addr >> 8 | BbEeProg.WRITE_DISABLE_BIT)


@pytest.mark.skip(reason="manual")
def test_timing():
    """Ensure write timing

    Verifying that we don't write too fast:
    Using a signal analyzer and PulseView we can measure the timing of the nWE
    signal and export it as csv. The report will contain the duration of nWE
    being low and high in micro seconds.

    Protocol decoder "Timing" setup:
    - Format of 'time' annotation: terse-us
    - Averaging period: 0 (disables averaging)
    - Edges to check: any
    New View: Tabular Decoder Output View
    - Export as csv
    """
    import csv

    with open("write_enable_timing.csv", "r", newline="") as f:
        # quick and dirty csv read
        r = csv.reader(f, delimiter=",", quotechar='"')
        r.__next__()  # skip header
        data = []
        for row in r:
            assert len(row) == 7
            assert row[2] == "Timing"
            data.append(int(row[5]))

        # ensure write enable is low >= 10 ms
        nwe_high_us = 3 * 1000  # arbitrary, makes detecting writes easier
        nwe_low_us = 12 * 1000

        min_nwe_low = 9999999999
        max_nwe_high = 0

        for i in range(len(data) - 3 + 1):
            print(data[i : i + 3])
            assert data[i] <= nwe_high_us or data[i] >= nwe_low_us
            if data[i] <= nwe_high_us:
                assert data[i + 1] >= nwe_low_us
                assert data[i + 2] <= nwe_high_us
                min_nwe_low = min(min_nwe_low, data[i + 1])
                max_nwe_high = max(max_nwe_high, max(data[i], data[i + 2]))

        # compare with permitted values in data sheet
        print(f"Min nWE low = {min_nwe_low} us")
        print(f"Max nWE high = {max_nwe_high} us")
