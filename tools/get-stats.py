#!/usr/bin/python3

import argparse
import datetime
import glob
import itertools
import json
import os
import re
import socket
import subprocess
import sys

try:
    import psutil
except ImportError:
    psutil = None
    print('No psutil, process information will not be included',
          file=sys.stderr)

try:
    import pymysql
except ImportError:
    pymysql = None
    print('No pymysql, database information will not be included',
          file=sys.stderr)

# https://www.elastic.co/blog/found-crash-elasticsearch#mapping-explosion


def tryint(value):
    try:
        return int(value)
    except (ValueError, TypeError):
        return value


def get_service_stats(service):
    stats = {'MemoryCurrent': 0}
    output = subprocess.check_output(['/usr/bin/systemctl', 'show', service] +
                                     ['-p%s' % stat for stat in stats])
    for line in output.decode().split('\n'):
        if not line:
            continue
        stat, val = line.split('=')
        stats[stat] = tryint(val)

    return stats


def get_services_stats():
    services = [os.path.basename(s) for s in
                glob.glob('/etc/systemd/system/devstack@*.service')]
    return [dict(service=service, **get_service_stats(service))
            for service in services]


def get_process_stats(proc):
    cmdline = proc.cmdline()
    if 'python' in cmdline[0]:
        cmdline = cmdline[1:]
    return {'cmd': cmdline[0],
            'pid': proc.pid,
            'args': ' '.join(cmdline[1:]),
            'rss': proc.memory_info().rss}


def get_processes_stats(matches):
    me = os.getpid()
    procs = psutil.process_iter()

    def proc_matches(proc):
        return me != proc.pid and any(
            re.search(match, ' '.join(proc.cmdline()))
            for match in matches)

    return [
        get_process_stats(proc)
        for proc in procs
        if proc_matches(proc)]


def get_db_stats(host, user, passwd):
    dbs = []
    db = pymysql.connect(host=host, user=user, password=passwd,
                         database='performance_schema',
                         cursorclass=pymysql.cursors.DictCursor)
    with db:
        with db.cursor() as cur:
            cur.execute(
                'SELECT COUNT(*) AS queries,current_schema AS db FROM '
                'events_statements_history_long GROUP BY current_schema')
            for row in cur:
                dbs.append({k: tryint(v) for k, v in row.items()})
    return dbs


def get_http_stats_for_log(logfile):
    stats = {}
    for line in open(logfile).readlines():
        m = re.search('"([A-Z]+) /([^" ]+)( HTTP/1.1)?" ([0-9]{3}) ([0-9]+)',
                      line)
        if m:
            method = m.group(1)
            path = m.group(2)
            status = m.group(4)
            size = int(m.group(5))

            try:
                service, rest = path.split('/', 1)
            except ValueError:
                # Root calls like "GET /identity"
                service = path
                rest = ''

            stats.setdefault(service, {'largest': 0})
            stats[service].setdefault(method, 0)
            stats[service][method] += 1
            stats[service]['largest'] = max(stats[service]['largest'], size)

    # Flatten this for ES
    return [{'service': service, 'log': os.path.basename(logfile),
             **vals}
            for service, vals in stats.items()]


def get_http_stats(logfiles):
    return list(itertools.chain.from_iterable(get_http_stats_for_log(log)
                                              for log in logfiles))


def get_report_info():
    return {
        'timestamp': datetime.datetime.now().isoformat(),
        'hostname': socket.gethostname(),
    }


if __name__ == '__main__':
    process_defaults = ['privsep', 'mysqld', 'erlang', 'etcd']
    parser = argparse.ArgumentParser()
    parser.add_argument('--db-user', default='root',
                        help=('MySQL user for collecting stats '
                              '(default: "root")'))
    parser.add_argument('--db-pass', default=None,
                        help='MySQL password for db-user')
    parser.add_argument('--db-host', default='localhost',
                        help='MySQL hostname')
    parser.add_argument('--apache-log', action='append', default=[],
                        help='Collect API call stats from this apache log')
    parser.add_argument('--process', action='append',
                        default=process_defaults,
                        help=('Include process stats for this cmdline regex '
                              '(default is %s)' % ','.join(process_defaults)))
    args = parser.parse_args()

    data = {
        'services': get_services_stats(),
        'db': pymysql and args.db_pass and get_db_stats(args.db_host,
                                                        args.db_user,
                                                        args.db_pass) or [],
        'processes': psutil and get_processes_stats(args.process) or [],
        'api': get_http_stats(args.apache_log),
        'report': get_report_info(),
    }

    print(json.dumps(data, indent=2))