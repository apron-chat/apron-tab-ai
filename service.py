"""Best-effort single-instance service; disk contains safe metadata only."""
import argparse
import asyncio
from contextlib import contextmanager, suppress
from decimal import Decimal
import fcntl
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import resource
import selectors
import signal
import subprocess
import sys
import tempfile
import threading
import time
import secrets

import bot

DEFAULT_STATE = Path('/workspace/apron-service')
RECONNECT_BASE_SECONDS = 5
RESTARTABLE = {'connection_closed', 'connection_unavailable', 'api_timeout',
               'api_transport', 'api_parsing', 'api_rate_limit', 'api_http_transient'}
EVENTS = RESTARTABLE | {
    'supervisor_started', 'worker_started', 'worker_exit', 'heartbeat',
    'signal_received', 'shutdown_requested', 'shutdown_complete', 'restart_scheduled',
    'prior_unclean_exit', 'service_internal_failure', 'websocket_open', 'websocket_closed',
    'websocket_handshake_failure', 'worker_heartbeat', 'worker_orphaned',
    'deployment_invalid', 'deployment_not_clean', 'singleton_required', 'service_error',
    'ready', 'call_reserved', 'reply_sent', 'api_backoff', 'api_failure',
    'model_output_metadata', 'operator_stop', 'hard_stop', 'budget_limit',
    'api_authentication', 'api_http', 'api_tls', 'api_unknown', 'operation_failed',
    'session_finished', 'ledger_invalid', 'configuration_missing', 'configuration_invalid',
    'base_url_rejected', 'budget_invalid', 'limits_invalid', 'human_allowlist_invalid',
    'protocol_or_auth_unsupported', 'authentication_failed', 'apron_request_rejected',
    'room_not_joined', 'room_selection_required', 'explicit_rooms_unsupported',
    'default_room_unresolved', 'resume_room_unresolved', 'resume_history_required',
    'history_invalid', 'pricing_invalid', 'pricing_unavailable', 'api_response_too_large',
    'model_output_model_mismatch', 'model_output_tool_call', 'model_output_secret_guard',
    'model_output_finish_rejected', 'model_output_empty_or_nontext',
    'model_output_unknown_structure', 'frame_rejected', 'protocol_invalid',
    'apron_server_error', 'server_configuration_changed', 'identity_changed',
    'startup_metadata_limit', 'ping_interval_invalid', 'call_limit', 'runtime_limit',
}
COUNTS = {'authenticated', 'room_selected', 'history_loaded', 'hello_acknowledged',
          'ready', 'eligible', 'calls', 'replies', 'rate_dropped', 'dropped',
          'reply_lookups', 'reply_lookup_failed', 'api_failures', 'backoff_seconds',
          'backoff_dropped', 'token_rotation', 'joined_rooms', 'reference_failures', 'participant_metadata_unavailable'}


def atomic_json(path, data):
    path = Path(path)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix='.atomic-', delete=False) as f:
            temp = Path(f.name)
            os.chmod(temp, 0o600)
            json.dump(data, f, separators=(',', ':'))
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temp:
            with suppress(FileNotFoundError):
                temp.unlink()


class Ledger:
    """No reset or refund; update is serialized and durable before inference."""
    def __init__(self, state):
        self.state = Path(state)
        self.path = self.state / 'budget.json'

    @contextmanager
    def locked(self):
        with (self.state / 'budget.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def read(self):
        try:
            data = json.loads(self.path.read_text())
            if set(data) != {'version', 'cumulative_reserved_usd', 'hello_claimed', 'attempts'}:
                raise ValueError
            total = Decimal(data['cumulative_reserved_usd'])
            if (data['version'] != 1 or not total.is_finite()
                    or not bot.KNOWN_PRIOR_RESERVATION_USD <= total <= Decimal('5')
                    or type(data['hello_claimed']) is not bool
                    or type(data['attempts']) is not int or data['attempts'] < 0):
                raise ValueError
            return data
        except Exception:
            raise bot.Stop('ledger_invalid') from None

    @property
    def total(self):
        return Decimal(self.read()['cumulative_reserved_usd'])

    def reserve(self, amount):
        with self.locked():
            data = self.read()
            if not amount.is_finite() or amount < 0:
                raise bot.Stop('budget_invalid')
            total = Decimal(data['cumulative_reserved_usd']) + amount
            if total > 5:
                raise bot.Stop('budget_limit')
            data['cumulative_reserved_usd'] = str(total)
            data['attempts'] += 1
            try:
                atomic_json(self.path, data)
            except Exception:
                raise bot.Stop('ledger_invalid') from None

    def claim_hello(self):
        with self.locked():
            data = self.read()
            if data['hello_claimed']:
                return False
            # Claim before sending: an ambiguous send is not repeated on restart.
            data['hello_claimed'] = True
            try:
                atomic_json(self.path, data)
            except Exception:
                raise bot.Stop('ledger_invalid') from None
            return True


def initialize(state, seed):
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(state, 0o700)
    if not seed.is_finite() or not bot.KNOWN_PRIOR_RESERVATION_USD <= seed < 5:
        raise bot.Stop('budget_invalid')
    # The marker survives ledger deletion/corruption. Initialization cannot reset it.
    try:
        fd = os.open(state / 'initialized', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise bot.Stop('already_initialized') from None
    os.fsync(fd)
    os.close(fd)
    atomic_json(state / 'budget.json', {'version': 1, 'cumulative_reserved_usd': str(seed),
                                      'hello_claimed': False, 'attempts': 0})


def singleton(state):
    lock = (state / 'service.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise bot.Stop('already_running') from None
    return lock


def process_start(pid):
    try:
        # No cmdline/environment inspection; start ticks prevent PID-reuse signals.
        fields = Path(f'/proc/{int(pid)}/stat').read_text().rsplit(')', 1)[1].split()
        if fields[0] == 'Z':
            return None
        return int(fields[19])
    except Exception:
        return None


def safe_event(line):
    """Drop unrecognized data before any disk write, including exception strings."""
    try:
        if len(line) > 16384:
            return None
        raw = json.loads(line)
        if not isinstance(raw, dict) or raw.get('status') not in EVENTS:
            return None
        out = {'status': raw['status']}
        if isinstance(raw.get('counts'), dict):
            out['counts'] = {k: v for k, v in raw['counts'].items()
                             if k in COUNTS and type(v) is int and 0 <= v <= 10**12}
        for key, choices in {
            'phase': {'pricing', 'completion', 'unknown'},
            'category': {'http', 'rate_limit', 'authentication', 'timeout', 'transport', 'parsing', 'tls', 'unknown'},
            'finish_reason': {'stop', 'length', 'tool_calls', 'content_filter', 'function_call', 'unknown'},
            'content_type': {'text', 'null', 'other'},
        }.items():
            if isinstance(raw.get(key), str) and raw[key] in choices:
                out[key] = raw[key]
        for key in ('recoverable', 'content_nonempty'):
            if type(raw.get(key)) is bool:
                out[key] = raw[key]
        for key in ('http_status', 'output_length', 'reasoning_tokens', 'signal', 'close_code',
                    'pid', 'start_ticks', 'boot_id', 'worker_boot_id', 'backoff_seconds',
                    'restart_count', 'max_rss_kib', 'cpu_ms'):
            if type(raw.get(key)) is int and 0 <= raw[key] < 2**63:
                out[key] = raw[key]
        if type(raw.get('exit_code')) is int and -255 <= raw['exit_code'] <= 255:
            out['exit_code'] = raw['exit_code']
        if raw.get('reason') in EVENTS:
            out['reason'] = raw['reason']
        return out
    except Exception:
        return None


class OperationalLog:
    """Only sanitized enums/numbers reach disk; bounded, flushed and fsynced."""
    def __init__(self, state, max_bytes=65536):
        self.state = Path(state)
        self.boot_id = secrets.randbits(62)
        self.handler = RotatingFileHandler(self.state / 'metadata.log', maxBytes=max_bytes, backupCount=3)
        self.handler.setFormatter(logging.Formatter('%(message)s'))
        self.handler.handleError = lambda record: None  # Never print a logging traceback/payload.

    def emit(self, status, **values):
        event = safe_event(json.dumps({'status': status, **values}))
        if event is None:
            return
        event.update(utc_epoch=int(time.time()), monotonic_ms=int(time.monotonic() * 1000),
                     boot_id=self.boot_id)
        self.handler.emit(logging.makeLogRecord({'msg': json.dumps(event), 'args': ()}))
        self.handler.flush()
        os.fsync(self.handler.stream.fileno())

    def begin(self):
        previous = None
        with suppress(Exception):
            previous = json.loads((self.state / 'lifecycle.json').read_text())
        if isinstance(previous, dict) and previous.get('clean_exit') is False:
            self.emit('prior_unclean_exit')
        elif previous is None:
            with suppress(Exception):
                old = json.loads((self.state / 'status.json').read_text())
                if old.get('state') in {'ready', 'starting', 'reconnect_backoff'}:
                    self.emit('prior_unclean_exit')
        atomic_json(self.state / 'lifecycle.json', {'boot_id': self.boot_id, 'clean_exit': False,
                    'pid': os.getpid(), 'start_ticks': process_start(os.getpid()), 'utc_epoch': int(time.time())})
        self.emit('supervisor_started', pid=os.getpid(), start_ticks=process_start(os.getpid()))

    def finish(self):
        self.emit('shutdown_complete')
        atomic_json(self.state / 'lifecycle.json', {'boot_id': self.boot_id, 'clean_exit': True,
                    'pid': os.getpid(), 'start_ticks': process_start(os.getpid()), 'utc_epoch': int(time.time())})

    def close(self):
        self.handler.close()


def resource_metrics():
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {'max_rss_kib': int(usage.ru_maxrss), 'cpu_ms': int((usage.ru_utime + usage.ru_stime) * 1000)}


def deployment(state, create=False):
    root = Path(__file__).resolve().parent
    if create:
        head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, stderr=subprocess.DEVNULL).decode().strip()
        dirty = subprocess.check_output(['git', 'status', '--porcelain'], cwd=root, stderr=subprocess.DEVNULL)
        if dirty or not re.fullmatch('[0-9a-f]{40}', head):
            raise bot.Stop('deployment_not_clean')
        atomic_json(state / 'deployment.json', {'root': str(root), 'commit': head})
    try:
        data = json.loads((state / 'deployment.json').read_text())
        if set(data) != {'root', 'commit'} or data['root'] != str(root) or not re.fullmatch('[0-9a-f]{40}', data['commit']):
            raise ValueError
        head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, stderr=subprocess.DEVNULL).decode().strip()
        dirty = subprocess.check_output(['git', 'status', '--porcelain'], cwd=root, stderr=subprocess.DEVNULL)
        if head != data['commit'] or dirty:
            raise ValueError
        return data
    except Exception:
        raise bot.Stop('deployment_invalid') from None


def validate_lock(state, fd):
    try:
        if fd is None or os.fstat(fd).st_ino != (state / 'service.lock').stat().st_ino:
            raise ValueError
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except Exception:
        raise bot.Stop('singleton_required') from None


def worker(state):
    logging.disable(logging.CRITICAL)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    parent, stamp = os.getppid(), process_start(os.getppid())
    boot = secrets.randbits(62)
    print(json.dumps({'status': 'worker_started', 'worker_boot_id': boot,
                      'pid': os.getpid(), 'start_ticks': process_start(os.getpid())}), flush=True)

    def orphan_watch():
        while True:
            time.sleep(2)
            if process_start(parent) != stamp:
                print('{"status":"worker_orphaned"}', flush=True)
                os.kill(os.getpid(), signal.SIGTERM)
                return

    def heartbeat():
        while True:
            time.sleep(30)
            print(json.dumps({'status': 'worker_heartbeat', 'worker_boot_id': boot, **resource_metrics()}), flush=True)

    threading.Thread(target=orphan_watch, daemon=True).start()
    threading.Thread(target=heartbeat, daemon=True).start()
    ledger = Ledger(state)
    env = dict(os.environ)  # In-memory inheritance only; never serialize credentials.
    env['BOT_PRIOR_SPEND_USD'] = str(ledger.total)
    env['BOT_BUDGET_USD'] = str(Decimal('5') - ledger.total)
    config = bot.Config.from_env(env)
    config.service_mode, config.ledger = True, ledger
    reason = asyncio.run(bot.live(config))
    return 75 if reason in RESTARTABLE else 0 if reason == 'operator_stop' else 2


def supervise(state, lock_fd):
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    ledger = Ledger(state)
    deploy = deployment(state)
    ledger.read()
    stopped = threading.Event()
    received_signal = [0]
    def stop_signal(signum, frame):
        received_signal[0] = signum
        stopped.set()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop_signal)
    operational = OperationalLog(state)
    operational.begin()
    record = {'state': 'starting', 'supervisor_pid': os.getpid(), 'supervisor_start': process_start(os.getpid()),
              'worker_pid': None, 'worker_start': None, 'commit': deploy['commit'],
              'restarts': 0, 'ready': False, 'counts': {}, 'boot_id': operational.boot_id, 'clean_exit': False}

    def save():
        record['updated_utc_epoch'] = int(time.time())
        record['cumulative_reserved_usd'] = str(ledger.total)
        record['reserved_attempts'] = ledger.read()['attempts']
        atomic_json(state / 'status.json', record)

    def accept(line):
        event = safe_event(line)
        if event is None:
            return
        operational.emit(**event)
        record['last_event'] = event['status']
        if event['status'] not in {'heartbeat', 'worker_heartbeat'}:
            record['last_operational_event'] = event['status']
        if event['status'] in RESTARTABLE or event['status'] in {'api_failure', 'websocket_closed', 'websocket_handshake_failure'}:
            record['last_error'] = event
        if event['status'] == 'worker_heartbeat':
            record['worker_heartbeat_utc'] = int(time.time())
        if 'worker_boot_id' in event:
            record['worker_boot_id'] = event['worker_boot_id']
        if 'counts' in event:
            record['counts'] = event['counts']
        if event['status'] == 'ready':
            record.update(state='ready', ready=True)
        if event['status'] in {'websocket_closed', 'connection_unavailable', 'connection_closed'}:
            record['ready'] = False
        save()

    delay = RECONNECT_BASE_SECONDS
    child = None
    try:
        while not stopped.is_set():
            if (state / 'stop.request').exists():
                break
            deployment(state)
            ledger.read()
            record.update(state='starting', ready=False, counts={})
            child = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), 'worker', '--state', str(state), '--lock-fd', str(lock_fd)],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                     pass_fds=(lock_fd,), cwd=deploy['root'])
            record.update(worker_pid=child.pid, worker_start=process_start(child.pid))
            operational.emit('worker_started', pid=child.pid, start_ticks=record['worker_start'])
            save()
            started = time.monotonic()
            next_heartbeat = started
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                buffer = b''
                while child.poll() is None:
                    if stopped.is_set() or (state / 'stop.request').exists():
                        operational.emit('shutdown_requested', signal=received_signal[0])
                        if received_signal[0]:
                            operational.emit('signal_received', signal=received_signal[0])
                        child.terminate()
                        try:
                            child.wait(timeout=7)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait()
                        stopped.set()
                        break
                    for key, _ in selector.select(timeout=.5):
                        data = os.read(key.fd, 16384)
                        buffer += data
                        while b'\n' in buffer:
                            line, buffer = buffer.split(b'\n', 1)
                            accept(line)
                        if len(buffer) > 16384:
                            buffer = b''
                    if time.monotonic() >= next_heartbeat:
                        operational.emit('heartbeat', pid=os.getpid(), **resource_metrics())
                        record['heartbeat_utc'] = int(time.time())
                        next_heartbeat = time.monotonic() + 30
                    save()
                for line in child.stdout.read(65536).splitlines():
                    accept(line)
            code = child.wait()
            record['worker_exit_code'] = code
            record['worker_exit_signal'] = -code if code < 0 else 0
            reason = record.get('last_operational_event', 'operation_failed')
            operational.emit('worker_exit', exit_code=code, signal=-code if code < 0 else 0,
                             reason=reason if reason in EVENTS else 'operation_failed')
            child.stdout.close()
            record.update(worker_pid=None, worker_start=None, ready=False)
            if stopped.is_set() or code != 75:
                record['state'] = 'stopped' if stopped.is_set() else 'halted'
                break
            record['restarts'] += 1
            record['state'] = 'reconnect_backoff'
            record['backoff_seconds'] = delay
            operational.emit('restart_scheduled', backoff_seconds=delay, restart_count=record['restarts'],
                             reason=reason if reason in EVENTS else 'operation_failed')
            save()
            if time.monotonic() - started > 60:
                delay = RECONNECT_BASE_SECONDS
            until = time.monotonic() + delay
            while time.monotonic() < until and not stopped.wait(.5):
                if (state / 'stop.request').exists():
                    stopped.set()
            delay = min(300, delay * 2)
        if stopped.is_set() or (state / 'stop.request').exists():
            record.update(state='stopped', ready=False)
        save()
    except Exception:
        record.update(state='halted', ready=False, last_event='service_internal_failure')
        with suppress(Exception):
            operational.emit('service_internal_failure')
            save()
    finally:
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=7)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        with suppress(Exception):
            record.update(clean_exit=True, ready=False)
            if received_signal[0]:
                record['shutdown_signal'] = received_signal[0]
            save()
            operational.finish()
        operational.close()


def status(state):
    ledger = Ledger(state)
    total = ledger.total
    try:
        saved = json.loads((state / 'status.json').read_text())
    except FileNotFoundError:
        saved = {'state': 'not_started'}
    for name in ('supervisor', 'worker'):
        saved[name + '_alive'] = (type(saved.get(name + '_pid')) is int
                                  and process_start(saved[name + '_pid']) == saved.get(name + '_start'))
    saved['cumulative_reserved_usd'] = str(total)
    saved['reserved_attempts'] = ledger.read()['attempts']
    if saved.get('ready') and not saved['worker_alive']:
        saved['ready'] = False
    age = max(0, int(time.time()) - saved.get('updated_utc_epoch', 0))
    saved['status_age_seconds'] = age
    saved['status_stale'] = age > 90
    if saved['status_stale'] or not saved['supervisor_alive']:
        saved['ready'] = False
    if not saved['supervisor_alive'] and not saved['worker_alive']:
        if saved.get('clean_exit') is not True and saved.get('state') not in {'stopped', 'halted', 'not_started'}:
            saved['state'] = 'unclean_stop'
            saved['diagnosis'] = 'unknown_abrupt_stop_environment_loss_possible'
        else:
            saved['diagnosis'] = 'recorded_clean_exit' if saved.get('clean_exit') else 'not_running'
    else:
        saved['diagnosis'] = 'stale_heartbeat' if saved['status_stale'] else 'running'
    return saved


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['init', 'deploy', 'start', 'status', 'stop', 'supervise', 'worker'])
    parser.add_argument('--state', type=Path, default=DEFAULT_STATE)
    parser.add_argument('--seed', default=str(bot.KNOWN_PRIOR_RESERVATION_USD))
    parser.add_argument('--lock-fd', type=int)
    args = parser.parse_args()
    state = args.state.resolve()
    try:
        if args.action == 'init':
            initialize(state, Decimal(args.seed))
            deployment(state, create=True)
            print(json.dumps({'status': 'initialized', 'cumulative_reserved_usd': str(Ledger(state).total)}))
        elif args.action == 'deploy':
            with singleton(state):
                Ledger(state).read()
                data = deployment(state, create=True)
            print(json.dumps({'status': 'deployment_pinned', 'commit': data['commit']}))
        elif args.action == 'start':
            Ledger(state).read()
            deployment(state)
            lock = singleton(state)
            with suppress(FileNotFoundError):
                (state / 'stop.request').unlink()
            with open(os.devnull, 'wb') as null:
                child = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), 'supervise',
                                          '--state', str(state), '--lock-fd', str(lock.fileno())],
                                         pass_fds=(lock.fileno(),), stdin=subprocess.DEVNULL,
                                         stdout=null, stderr=null, start_new_session=True)
            lock.close()
            print(json.dumps({'status': 'supervisor_started', 'supervisor_pid': child.pid}))
        elif args.action == 'status':
            print(json.dumps(status(state)))
        elif args.action == 'stop':
            Ledger(state).read()
            atomic_json(state / 'stop.request', {'stop': True})
            saved = status(state)
            if saved['supervisor_alive']:
                os.kill(saved['supervisor_pid'], signal.SIGTERM)
            elif saved['worker_alive']:
                os.kill(saved['worker_pid'], signal.SIGTERM)
            print(json.dumps({'status': 'stop_requested'}))
        elif args.action == 'supervise':
            validate_lock(state, args.lock_fd)
            supervise(state, args.lock_fd)
        else:
            validate_lock(state, args.lock_fd)
            return worker(state)
        return 0
    except bot.Stop as exc:
        code = str(exc)
        if code not in EVENTS | {'already_initialized', 'already_running', 'deployment_invalid', 'deployment_not_clean', 'singleton_required'}:
            code = 'service_error'
        print(json.dumps({'status': code}), flush=True)
        if args.action in {'start', 'supervise'} and state.is_dir():
            with suppress(Exception):
                log = OperationalLog(state)
                log.emit(code)
                log.close()
        return 2
    except Exception:
        print('{"status":"service_error"}', flush=True)
        if args.action in {'start', 'supervise'} and state.is_dir():
            with suppress(Exception):
                log = OperationalLog(state)
                log.emit('service_error')
                log.close()
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
