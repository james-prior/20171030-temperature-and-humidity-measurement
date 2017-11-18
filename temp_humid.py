#!/usr/bin/env python3
#
# $Id$
# $Revision$
#
# <daemon.py>
#
# Description:
#
# This is a class which allows a python script to
# treated as a deamon.
#

import argparse
import logging
import logging.handlers
import sys
import os
import time
import atexit
import os.path
from datetime import datetime
from datetime import timedelta
import re
import ftplib
import threading
import queue
import socket
from functools import partial

import serial
import matplotlib.pyplot as plt
import matplotlib.animation as animation
import matplotlib.dates as mdates

SERIAL_PORT_BIT_RATE = 9600  # Unit is 1 bit per second.

graphqueue = None
ftpqueue = None

record_pattern = re.compile(
    br'^\s*(?P<device_id>[0-9a-fA-F]{16,})\s+(?P<csv_fields>.+)$')

def get_bytes_from_serial_port(serial_port, n, timeout):
    serial_port.timeout = 1.0
    start_time = time.time()
    for data in iter(partial(serial_port.read, n), None):
        now = time.time()
        if data:
            yield data
            start_time = now
        elif now - start_time > timeout:
            return


def get_lines(iterable_of_bytes):
    for data in iterable_of_bytes:
        try:
            before_eod, _ = data.split(b'EOD', 1)
        except ValueError:
            continue
        lines = before_eod.split(b'\n')
        yield from lines


def recorder(serial_port):
    """ Collect ASCII data from the LinkTH controller, producing measurements

        This function interprets data flowing from the controller, trying
        to produce measurements records. Each measurement record has the
        following format:

        hour      -- int:    The local hour of the day
        date_time -- string: YYYY-mm-dd HH:MM:SS time of day
        ident     -- iButton (oneWire) identification, 16 hex digits
        type      -- LinkTH iButton "type" string
        vvvvvvvvv Device-dependent variable fields
        tempC     -- Temperature Celsius if device is a temp/humidity sensor
                     (type=19)
        tempF     -- Temperature Fahrenheit if device is a temp/humidity sensor
                     (type=19)
        humid     -- Percent humidity if device is a temp/humidity sensor
                     (type=19)
        ^^^^^^^^^
        dtime     -- device reported time
        seconds   -- Data collection host time in seconds since 1/1/1970 UTC

        Note that for devices other than type=19, the fields tempC, tempF and
        humid will be replaced by possible different number of fields whose
        content is specific to the reporting device.

        Any interpreting script/function that examines measurements records
        should take the ident/type into consideratio unless it is known that
        the iBuffonLink string of devices is known.
    """
    for line in get_lines(get_bytes_from_serial_port(
            serial_port, n=500, timeout=30)):
        if len(line) < 20:
            continue
        line = line.strip(b'\r').strip(b'?')
        matcher = record_pattern.match(line)
        if not matcher:
            continue
        d = {
            key: value.decode(encoding="ascii", errors="none")
            for key, value in matcher.groupdict().items()}
        ident = d['device_id']
        now = time.time()
        timeobj = time.localtime(now)
        nowstr = time.strftime('%Y-%m-%d %H:%M:%S', timeobj)
        measurements = [timeobj.tm_hour, nowstr, ident]
        fields = d['csv_fields'].split(',')
        #!!! What is there are more or less fields than expected?
        measurements.extend([field.strip() for field in fields])
        timesecs = str(int(now))
        measurements.append(timesecs)
        yield measurements

    logging.info("timeout")
    return b'Timeout'

def record_cycle(portname, outfile, graphqueue):
    """ Get and handle measurement data from the iButtonLink LinkTH controller.

        Manage distribution of the data to the graphqueue, and manage
        starts/stops and errors detected by the recorder.
    """
    start_hour = time.localtime().tm_hour
    try:
        sport = serial.Serial(portname, baudrate=SERIAL_PORT_BIT_RATE)
        for measurement in recorder(sport):
            print(' '.join(measurement[1:]), file=outfile)
            outfile.flush()
            graphqueue.put(measurement)
            if measurement[0] != start_hour:
                break
    except serial.serialutil.SerialException as e:
        raise e
    except KeyboardInterrupt:
        return outfile
    finally:
        try:
            sport.close()
        except:
            pass
    return

def record_thread(recordqueue, graphqueue, datadir, portname, portretry):
    """ entry point for the data recorder, collector, distribution thread

        Manage report files and notification to the main thread for
        triggering FTP transfers.
    """
    logging.info("record_thread start dir=%s, port=%s", datadir, portname)
    while True:
        # Start recording
        outfilename = time.strftime('temp_monitor_%Y-%m-%d_%H.txt')
        outfilepath = os.path.join(datadir, outfilename)
        logging.info('outfile %s', outfilepath)
        outfile = open(outfilepath, 'a')
        try:
            recordqueue.put( (outfilepath, outfilename, "start") )
            record_cycle(portname, outfile, graphqueue)
            recordqueue.put( (outfilepath, outfilename, "end") )
        except serial.serialutil.SerialException:
            recordqueue.put( (outfilepath, outfilename, "error") )
            logging.critical("Warning: Serial port %s unavailable", portname)
            time.sleep(int(portretry))
        outfile.close()

def generator():
    measurements = []
    while True:
        try:
            measurements.append(graphqueue.get(block=False, timeout=None))
        except queue.Empty:
            logging.debug("generator: %s", measurements)
            yield measurements
            measurements = []


x = []
temp_y = []
humid_y = []
thresh_y = []
thresh_line = None
temp_line = None
humid_line = None
humid_ax = None
temp_ax = None
time_count = 0
chart_interval = None
chart_interval2 = None
MEAS_INTERVAL = 2
INTERVAL_BUFFER = 20
temp_color = None
humid_color = None
THRESH_COLOR = "red"
humidity_threshold = None
date_format = mdates.DateFormatter('%Y-%m-%d\n%H:%M:%S')
temp_units = None
def animate(measurements):
    global temp_line, humid_line, x, temp_y, humid_y, humid_ax, temp_ax
    global thresh_line, thresh_y
    global temp_line2, humid_line2, humid_ax2, temp_ax2, thresh_line2
    global humidity_threshold, chart_interval, chart_interval2, temp_color
    global humid_color, temp_units
    global date_format

    for measurement in measurements:
        logging.debug("animate: %s", measurement)
        try:
            (
                hour,
                date_time,  #!!! what is this date time of?
                device_id,
                device_type,
                temperature_c,
                temperature_f,
                rel_humidity,
                device_uptime_hh_mm_ss_t,  # HH:MM:SS.t
                host_time_epoch,  # Unit is one second since UNIX epoch.
            ) = measurement
        except ValueError:
            if x:
                x.append(x[-1])
                temp_y.append(temp_y[-1])
                humid_y.append(humid_y[-1])
                thresh_y.append(humidity_threshold)
        else:
            x.append(datetime.strptime(date_time, '%Y-%m-%d %H:%M:%S'))
            if temp_units == 'F':
                temp_y.append(temperature_f)
            else:
                temp_y.append(temperature_c)

            humid_y.append(rel_humidity)
            thresh_y.append(humidity_threshold)

    if not measurements:
        if len(x) == 0:
            return temp_line, humid_line, thresh_line
        else:
            x.append(x[-1])
            temp_y.append(temp_y[-1])
            humid_y.append(humid_y[-1])
            thresh_y.append(humidity_threshold)


    prune_count = len(x) - ((chart_interval//MEAS_INTERVAL) + INTERVAL_BUFFER)
    if prune_count > 0:
        x = x[prune_count :  ]
        temp_y = temp_y[prune_count :  ]
        humid_y = humid_y[prune_count :  ]
        thresh_y = thresh_y[prune_count :  ]

    if humid_y and int(humid_y[-1]) > humidity_threshold:
        plt.suptitle("HUMIDITY ALERT", fontsize=40, color="red")
    else:
        plt.suptitle("")

    now = x[-1]
    #logging.debug("animate now: %s", now)
    temp_ax.set_xlim(now - timedelta(0,chart_interval), now)
    temp_ax2.set_xlim(now - timedelta(0,chart_interval2), now)

    temp_ax.xaxis.set_major_formatter(date_format)
    temp_ax2.xaxis.set_major_formatter(date_format)

    temp_line.set_data(x, temp_y)  # update the data
    humid_line.set_data(x, humid_y)  # update the data
    thresh_line.set_data(x, thresh_y)

    temp_line2.set_data(x, temp_y)  # update the data
    humid_line2.set_data(x, humid_y)  # update the data
    thresh_line2.set_data(x, thresh_y)

    logging.debug('%s/%s/%s', len(x), len(temp_y), len(humid_y))
    return temp_line, humid_line, thresh_line

chart_height = None
chart_width = None
def init_charting():
    global temp_line, humid_line, humid_ax, temp_ax, thresh_line
    global temp_line2, humid_line2, humid_ax2, temp_ax2, thresh_line2
    global humidity_threshold, temp_color, humid_color, temp_units
    #fig = plt.figure(1)
    now = time.time()
    #logging.debug("now = %s", now)
    fig, axes = plt.subplots(2, 1, False, False)
    fig.set_size_inches(chart_width,chart_height)
    temp_ax = axes[0]
    #humid_ax = axes[1]
    #temp_ax = fig.gca()
    humid_ax = temp_ax.twinx()
    temp_ax.set_xlabel('Time')
    temp_ax.set_ylabel(
        "Temperature (" + temp_units + ")",
        color=temp_color,
        fontsize=16,
    )
    humid_ax.set_ylabel(
        "Relative Humidity (%)",
        color=humid_color,
        fontsize=16,
    )
    if temp_units == 'F':
        temp_ax.set_ylim(bottom=50, top=185)
    elif temp_units == 'C':
        temp_ax.set_ylim(bottom=10, top=85)
    else:
        logging.critical("Temp units not 'C' or 'F'")
        sys.exit(1)

    humid_ax.set_ylim(bottom=0, top=100)
    temp_ax.grid(True)
    temp_ax.tick_params(labelsize=8)
    temp_line, = temp_ax.plot([], [], temp_color, lw=2)
    humid_line, = humid_ax.plot([], [], humid_color, lw=2)
    thresh_line, = humid_ax.plot([], [], THRESH_COLOR, lw=2, ls='dashed')

    temp_ax2 = axes[1]
    humid_ax2 = temp_ax2.twinx()
    temp_ax2.set_xlabel('Time')
    temp_ax2.set_ylabel(
        "Temperature (" + temp_units + ")",
        color=temp_color,
        fontsize=16,
    )
    humid_ax2.set_ylabel(
        "Relative Humidity (%)",
        color=humid_color,
        fontsize=16,
    )
    if temp_units == 'F':
        temp_ax2.set_ylim(bottom=50, top=185)
    elif temp_units == 'C':
        temp_ax2.set_ylim(bottom=10, top=85)
    else:
        logging.critical("Temp units not 'C' or 'F'")
        sys.exit(1)

    humid_ax2.set_ylim(bottom=0, top=100)
    temp_ax2.grid(True)
    temp_ax2.tick_params(labelsize=8)
    temp_line2, = temp_ax2.plot([], [], temp_color, lw=2)
    humid_line2, = humid_ax2.plot([], [], humid_color, lw=2)
    thresh_line2, = humid_ax2.plot([], [], THRESH_COLOR, lw=2, ls='dashed')

    return fig

def ani_init():
    global temp_line, humid_line, thresh_line
    temp_line.set_data([],[])
    humid_line.set_data([],[])
    thresh_line.set_data([],[])

    temp_line2.set_data([],[])
    humid_line2.set_data([],[])
    thresh_line2.set_data([],[])
    return (
        temp_line,
        humid_line,
        thresh_line,
        temp_line2,
        humid_line2,
        thresh_line2,
    )

def ftpsend(filepath, filename):
    try:
        with ftplib.FTP(
                host="www.salientsystems.com",
                user="salient",
                passwd="space",
        ) as ftp:
            logging.info("ftp start")
            with open(filepath, 'rb') as ftpfile:
                ftp.cwd("test_temperature")
                ftp.storbinary("STOR %s" % filename, ftpfile)
                ftp.quit()
                logging.info("ftp complete")
    except socket.gaierror:
        logging.critical("Bad FTP host address")
    except socket.error:
        logging.critical("FTP socket error")
    except socket.herror:
        logging.critical("FTP socket H error")
    except ftplib.Error as ftpe:
        logging.critical("FTP error")

ftpperiod = None
def ftp_thread():
    global recordqueue, ftpperiod
    fileobj = None
    recordstate = None
    while True:
        try:
            fileobj = recordqueue.get(block=True, timeout=ftpperiod)
            if fileobj[2] == "end":
                recordstate = "Complete"
            else:
                recordstate = None
        except queue.Empty:
            recordstate = "timeout"
            pass
        logging.info("main thread")
        if fileobj is not None and recordstate is not None:
            ftpsend(fileobj[0], fileobj[1])

def main():
    global graphqueue, recordqueue, ftpperiod
    global humidity_threshold, chart_interval, chart_interval2, temp_color
    global humid_color, chart_width, chart_height, temp_units

    logging.basicConfig(
        level=logging.DEBUG,
        format='%(levelname)s: %(asctime)s %(message)s', 
	handlers=[
            logging.handlers.RotatingFileHandler(
                "/tmp/temp_humid.log",
                'a',
                maxBytes=50000,
                backupCount=4)])

    # check usage
    parser = argparse.ArgumentParser(
        prog=sys.argv[0],
        description=(
            "Collect temp and humidity data from a "
            "one-wire temp and humidity sensor, produce a live graph of it, "
            "and archive the data using FTP."
        ),
    )
    charting_group = parser.add_argument_group(
        'Charting parameters',
        "Parameters to control charting behavior.")
    charting_group.add_argument(
        "-cw",
        "--chartwidth",
        dest='chart_width',
        default="16",
        metavar="<chart width>",
        help="Chart width in inches.",
    )
    charting_group.add_argument(
        "-ch",
        "--chartheight",
        dest='chart_height',
        default="8",
        metavar="<chart height>",
        help="Chart height in inches.",
    )
    charting_group.add_argument(
        "-di",
        "--displayinterval",
        dest='display_interval',
        default="10000",
        metavar="<display interval>",
        help="Interval at which the display updates in ms.",
    )
    charting_group.add_argument(
        "-hc",
        "--humiditycolor",
        dest='humid_color',
        default="green",
        metavar="<humidity color>",
        help="Color of the humidity line on the chart.",
    )
    charting_group.add_argument(
        "-i",
        "--interval",
        dest='chart_interval',
        default='43200',
        metavar="<chart interval>",
        help="Time period (in seconds) on the x-axis of the chart.",
    )
    charting_group.add_argument(
        "-i2",
        "--interval_2",
        dest='chart_interval2',
        default='600',
        metavar="<secondary chart interval>",
        help=(
            "Time period (in seconds) on the x-axis of the secondary chart. "
            "Must be <= the primary chart interval"
        ),
    )
    charting_group.add_argument(
        "-th",
        "--thresh",
        dest='humidity_threshold',
        metavar="<humidity threshold>, ",
        default='10',
        help=(
            "Percent relative humidity at which a threshold line is drawn on "
            "chart.  A warning message is displayed when the humidity crosses "
            "this threshold"
        ),
    )
    charting_group.add_argument(
        "-tc",
        "--tempcolor",
        dest='temp_color',
        default="blue",
        metavar="<temp color>",
        help="Color of the temperature line on the chart.",
    )
    charting_group.add_argument(
        "-tu",
        "--tempunits",
        dest='temp_units',
        default="F",
        metavar="<temp units>",
        choices=['C', 'F'],
        help="Temperature units.  ",
    )
    args = parser.parse_args()

    humidity_threshold = int(args.humidity_threshold)
    chart_interval = int(args.chart_interval)
    chart_interval2 = int(args.chart_interval2)
    temp_color = args.temp_color
    humid_color = args.humid_color
    chart_width = int(args.chart_width)
    chart_height = int(args.chart_height)
    temp_units = args.temp_units
    display_interval = int(args.display_interval)
    logging.info("humidity_threshold = %s", humidity_threshold)
    logging.info("chart_interval = %s", chart_interval)
    logging.info("chart_interval2 = %s", chart_interval2)
    logging.info("temp_color = %s", temp_color)
    logging.info("humid_color = %s", humid_color)
    logging.info("chart_width = %s", chart_width)
    logging.info("chart_height = %s", chart_height)
    logging.info("temp_units = %s", temp_units)
    logging.info("display_interval = %s", display_interval)


    if chart_interval2 > chart_interval:
        logging.critical(
            "Secondary chart interval must be <= the primary chart interval")
        sys.exit(1)

    portname = (
        '/dev/serial/by-id/'
        'usb-Prolific_Technology_Inc._USB-Serial_Controller_D-if00-port0')
    portretry = 60
    datadir = '/tmp'
    ftpperiod = 600

    graphqueue = queue.Queue(maxsize=100)
    graphargs = (graphqueue,)

    recordqueue = queue.Queue(maxsize=10)
    recordargs = (recordqueue, graphqueue, datadir, portname, portretry)
    recordthread = threading.Thread(
        name="recordthread",
        target=record_thread,
        args=recordargs,
        daemon=1,
    )
    recordthread.start()

    ftpThread = threading.Thread(name="ftpthread", target=ftp_thread, daemon=1)
    ftpThread.start()

    # set up and run dynamic charting from the main loop
    fig = init_charting()
    ani = animation.FuncAnimation(
        fig,
        animate,
        frames=generator,
        init_func=ani_init,
        interval=display_interval,
        blit=False,
        repeat=False,
    )
    plt.show()

if __name__ == "__main__":
    main()
