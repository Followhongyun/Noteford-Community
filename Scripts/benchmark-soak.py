"""Isolated, wall-clock 10-second sync soak. No installed settings or user data.

PYTHONPATH=src python Scripts/benchmark-soak.py --root /absolute/isolated/path \
  --output /absolute/result/path --phase-seconds 450

Four phases: idle, 100 texts/10s, 2 media + 100 texts/10s with a 100-file
burst, 1000 texts/10s. Producer and throttled loopback HTTP server are separate
processes. Media session acquisition is replaced by synthetic transport;
actual queue, transfer/decryption/hash checks and cache retention run normally.
The default 2 MiB/s limit belongs only to the fixture HTTP server. Use
--media-bandwidth-mib 0 for an unthrottled source; the production media worker is used,
including its refresh priority. --image-size 2048 creates roughly 12 MiB PNGs.
--pressure-media repeats media work alongside the 1000-text/10s phase.
"""
import argparse
import ctypes
import ctypes.util
import hashlib
import hmac
import json
import os
import random
import resource
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zlib
from contextlib import closing
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

KEY = bytes(range(32))
SALT = b'fixture-salt-123'
IV = bytes(range(16))
ACCOUNT = 'wxid_fixture_1234'
CHATS = [f'fixture{i}@chatroom' for i in range(10)]
TABLES = ['Msg_' + hashlib.md5(c.encode()).hexdigest() for c in CHATS]
PHASES = ['idle', 'continuous-text', 'media-download', '1000-text-per-10s']
MAC_KEY = hashlib.pbkdf2_hmac('sha512', KEY, bytes(v ^ 0x3a for v in SALT), 2, dklen=32)


def atomic_json(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value))
    temp.replace(path)


def encrypt(page, number):
    enc = Cipher(algorithms.AES(KEY), modes.CBC(IV)).encryptor()
    encoded = (SALT if number == 1 else b'') + enc.update(page[16:4016] if number == 1 else page[:4016]) + enc.finalize() + IV
    digest = hmac.new(MAC_KEY, encoded[16:] + struct.pack('<I', 1), hashlib.sha512).digest() if number == 1 else bytes(64)
    return encoded + digest


def encrypt_database(source, target):
    temp = target.with_suffix('.tmp')
    with source.open('rb') as inp, temp.open('wb') as out:
        number = 1
        while page := inp.read(4096):
            out.write(encrypt(page, number))
            number += 1
    temp.replace(target)


def create_reserved(path, sql):
    lib = ctypes.CDLL(ctypes.util.find_library('sqlite3'))
    lib.sqlite3_open.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
    lib.sqlite3_file_control.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_void_p]
    lib.sqlite3_exec.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    lib.sqlite3_close.argtypes = [ctypes.c_void_p]
    db = ctypes.c_void_p()
    assert lib.sqlite3_open(os.fsencode(path), ctypes.byref(db)) == 0
    try:
        reserve = ctypes.c_int(80)
        assert lib.sqlite3_file_control(db, b'main', 38, ctypes.byref(reserve)) == 0
        assert lib.sqlite3_exec(db, ('PRAGMA page_size=4096;' + sql).encode(), None, None, None) == 0
    finally:
        lib.sqlite3_close(db)


def png_payload(size=512):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    rng = random.Random(42)
    raw = b''.join(b'\0' + rng.randbytes(size * 3) for _ in range(size))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')


def serve(root, *, bandwidth_mib=2, image_size=512):
    payload = png_payload(image_size)
    pad = 16 - len(payload) % 16
    enc = Cipher(algorithms.AES(KEY), modes.ECB()).encryptor()
    wire = enc.update(payload + bytes([pad]) * pad) + enc.finalize()
    atomic_json(root/'media-info.json', dict(size=len(payload), md5=hashlib.md5(payload).hexdigest(), encrypted_bytes=len(wire)))
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            begin, end = map(int, self.path.strip('/').split('-'))
            data = wire[begin:end+1]
            self.send_response(200)
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            for pos in range(0, len(data), 65536):
                part = data[pos:pos+65536]
                self.wfile.write(part)
                self.wfile.flush()
                if bandwidth_mib:
                    time.sleep(len(part) / (bandwidth_mib * 1024 * 1024))
    server = HTTPServer(('127.0.0.1', 0), Handler)
    atomic_json(root/'server.json', dict(port=server.server_port, pid=os.getpid()))
    server.serve_forever()


def produce(root, padding_mib, seed_rows, *, media_enabled=True, media_burst=100, pressure_media=False):
    original = root/'producer'
    original.mkdir(parents=True)
    storage = root/'wechat'/ACCOUNT/'db_storage'
    for category in ('contact', 'session', 'message'):
        (storage/category).mkdir(parents=True)
    contact_sql = '''CREATE TABLE contact(id INTEGER,username,alias,remark,nick_name,small_head_url,big_head_url);
        CREATE TABLE chat_room(id INTEGER,username,owner,ext_buffer BLOB);
        CREATE TABLE chatroom_member(room_id INTEGER,member_id INTEGER);
        INSERT INTO contact VALUES(1,'wxid_fixture','','','Fixture','','');'''
    contact_sql += ''.join(f"INSERT INTO contact VALUES({i+2},'{c}','','','Fixture {i}','','');" for i,c in enumerate(CHATS))
    session_sql = 'CREATE TABLE SessionTable(username,type,last_timestamp,sort_timestamp);CREATE TABLE SessionNoContactInfoTable(username,session_title);'
    session_sql += ''.join(f"INSERT INTO SessionTable VALUES('{c}',2,1,1);" for c in CHATS)
    message_sql = "CREATE TABLE Name2Id(user_name);INSERT INTO Name2Id VALUES('wxid_fixture');"
    for i, table in enumerate(TABLES):
        message_sql += f'''CREATE TABLE {table}(local_id INTEGER PRIMARY KEY,server_id INTEGER,create_time INTEGER,local_type INTEGER,message_content,real_sender_id INTEGER);
            WITH RECURSIVE n(x) AS(SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<{seed_rows//len(CHATS)})
            INSERT INTO {table} SELECT x,{i*100000000}+x,1,1,'synthetic historical '||printf('%0500d',x),1 FROM n;'''
    message_sql += f'CREATE TABLE padding(body BLOB);INSERT INTO padding VALUES(zeroblob({padding_mib*1024*1024}));'
    for category, name, sql in [('contact','contact.db',contact_sql),('session','session.db',session_sql),('message','message_0.db',message_sql)]:
        plain = original/name
        create_reserved(plain, sql)
        encrypt_database(plain, storage/category/name)
    plain = original/'message_0.db'
    destination = storage/'message/message_0.db'
    connection = sqlite3.connect(plain)
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('PRAGMA wal_autocheckpoint=0')
    connection.execute('PRAGMA synchronous=NORMAL')
    positions = [seed_rows//len(CHATS)] * len(CHATS)
    sent = seed_rows
    media_sent = 0
    copied_frames = 0
    generation = None
    checkpoint_count = 0
    def export_wal():
        nonlocal copied_frames, generation
        wal = Path(str(plain)+'-wal')
        if not wal.exists() or wal.stat().st_size <= 32:
            return
        with wal.open('rb') as inp:
            header = inp.read(32)
            if header != generation:
                generation = header
                copied_frames = 0
                Path(str(destination)+'-wal').write_bytes(header)
            count = (wal.stat().st_size-32)//4120
            inp.seek(32 + copied_frames*4120)
            with Path(str(destination)+'-wal').open('ab') as out:
                for _ in range(copied_frames, count):
                    frame = inp.read(24)
                    page = inp.read(4096)
                    number = struct.unpack('>I', frame[:4])[0]
                    out.write(frame + encrypt(page, number))
            copied_frames = count
    atomic_json(root/'producer-status.json', dict(ready=True, sent=sent, media_sent=0, pid=os.getpid(), source_bytes=destination.stat().st_size))
    while not (root/'control.json').exists():
        time.sleep(.1)
    control = json.loads((root/'control.json').read_text())
    start = control['start_monotonic']
    duration = control['phase_seconds']
    step = 0
    burst_phases = set()
    info = json.loads((root/'media-info.json').read_text())
    xml = f'<msg><img md5="{info["md5"]}" aeskey="{KEY.hex()}" cdnbigimgurl="{"ab"*32}" hdlength="{info["size"]}"/></msg>'
    try:
        while time.monotonic() < start + duration*4 and not (root/'stop-producer').exists():
            due = start + step
            if time.monotonic() < due:
                time.sleep(min(.1, due-time.monotonic()))
                continue
            phase = min(3, max(0, int((due-start)//duration)))
            texts = [0,10,10,100][phase]
            media_phase = phase == 2 or (pressure_media and phase == 3)
            media = (1 if step % 5 == 0 else 0) if media_phase and media_enabled else 0
            if media_phase and phase not in burst_phases and media_enabled:
                media += media_burst
                burst_phases.add(phase)
            rows = texts + media
            now = int(time.time())
            with connection:
                for i in range(rows):
                    chat = sent % len(CHATS)
                    positions[chat] += 1
                    is_media = i < media
                    connection.execute(f'INSERT INTO {TABLES[chat]} VALUES(?,?,?,?,?,1)',
                        (positions[chat],10000000000+sent,now,3 if is_media else 1,xml if is_media else 'synthetic new '+str(sent)+'x'*480))
                    sent += 1
                    media_sent += int(is_media)
            if rows:
                export_wal()
            if step and step % 60 == 0 and rows:
                connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                encrypt_database(plain, destination)
                Path(str(destination)+'-wal').unlink(missing_ok=True)
                generation = None
                copied_frames = 0
                checkpoint_count += 1
            atomic_json(root/'producer-status.json', dict(ready=True,sent=sent,media_sent=media_sent,step=step,phase=PHASES[phase],checkpoints=checkpoint_count,pid=os.getpid(),source_bytes=destination.stat().st_size))
            step += 1
    finally:
        connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        encrypt_database(plain, destination)
        Path(str(destination)+'-wal').unlink(missing_ok=True)
        atomic_json(root/'producer-status.json', dict(ready=True,done=True,sent=sent,media_sent=media_sent,step=step,checkpoints=checkpoint_count,pid=os.getpid(),source_bytes=destination.stat().st_size))
        connection.close()


class RusageV4(ctypes.Structure):
    _fields_ = [('uuid', ctypes.c_uint8*16)] + [(name,ctypes.c_uint64) for name in '''user_time system_time pkg_idle_wkups interrupt_wkups pageins wired_size resident_size phys_footprint proc_start_abstime proc_exit_abstime child_user_time child_system_time child_pkg_idle_wkups child_interrupt_wkups child_pageins child_elapsed_abstime diskio_bytesread diskio_byteswritten cpu_time_qos_default cpu_time_qos_maintenance cpu_time_qos_background cpu_time_qos_utility cpu_time_qos_legacy cpu_time_qos_user_initiated cpu_time_qos_user_interactive billed_system_time serviced_system_time logical_writes lifetime_max_phys_footprint instructions cycles billed_energy serviced_energy interval_max_phys_footprint runnable_time'''.split()]


LIBPROC = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
LIBPROC.proc_pid_rusage.argtypes = [ctypes.c_int,ctypes.c_int,ctypes.c_void_p]


def metrics():
    info = RusageV4()
    if LIBPROC.proc_pid_rusage(os.getpid(), 4, ctypes.byref(info)):
        raise OSError(ctypes.get_errno(), 'proc_pid_rusage')
    usage = resource.getrusage(resource.RUSAGE_SELF)
    import psutil
    proc = psutil.Process()
    return dict(cpu_seconds=usage.ru_utime+usage.ru_stime,rss_bytes=info.resident_size,
        footprint_bytes=info.phys_footprint,read_bytes=info.diskio_bytesread,
        write_bytes=info.diskio_byteswritten,logical_write_bytes=info.logical_writes,
        peak_rss_bytes=usage.ru_maxrss,threads=proc.num_threads(),fds=proc.num_fds())


def run(args):
    root, output = args.root.resolve(), args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    output.mkdir(parents=True, exist_ok=True)
    children = []
    stop = threading.Event()
    errors = []
    media_wake = threading.Event()
    media_thread = None
    def spawn(mode):
        log = (root/(mode+'.log')).open('w')
        process = subprocess.Popen([sys.executable,__file__,'--root',str(root),'--mode',mode,
            '--padding-mib',str(args.padding_mib),'--seed-rows',str(args.seed_rows),
            '--chat-count',str(args.chat_count),'--media-burst',str(args.media_burst),
            '--media-bandwidth-mib',str(args.media_bandwidth_mib),'--image-size',str(args.image_size),
            *(['--no-media'] if args.no_media else []),
            *(['--pressure-media'] if args.pressure_media else [])],stdout=log,stderr=subprocess.STDOUT)
        children.append(process)
        return process
    server, producer = spawn('server'), spawn('producer')
    try:
        deadline = time.monotonic()+180
        while not (root/'producer-status.json').exists() or not (root/'server.json').exists():
            if producer.poll() is not None or server.poll() is not None:
                raise RuntimeError('fixture helper failed; inspect isolated logs')
            if time.monotonic() > deadline:
                raise TimeoutError('fixture initialization')
            time.sleep(.1)
        from noteford import mac_live_sync as live, cdn_download as cdn
        from noteford.service import LocalApi
        from noteford.downloaded_media import retain_download, retained_download
        project = root/'project'
        state = project/'keycap'
        state.mkdir(parents=True)
        live.default_state_dir = lambda: state
        live.default_db_roots = lambda: [root/'wechat']
        keys = [dict(relative_path=f'{ACCOUNT}/db_storage/{category}/{name}',key_hex=KEY.hex())
            for category,name in [('contact','contact.db'),('session','session.db'),('message','message_0.db')]]
        live.keys_path(state).write_text(json.dumps(dict(keys=keys)))
        config = dict(project_root=str(project),index_path=str(project/'index.db'),
            wechat_root=str(live.managed_archive()),account=ACCOUNT,
            candidates_path=str(project/'synthetic-keys.json'),auto_media_loading=True,parse_stickers=False)
        core = LocalApi(config)
        core._automatic_wake = media_wake
        info = json.loads((root/'media-info.json').read_text())
        server_info = json.loads((root/'server.json').read_text())
        requests = []
        transfers = []
        def local_request(fields, deadline):
            begin,end = int(fields['rangestart']),int(fields['rangeend'])
            with urllib.request.urlopen(f'http://127.0.0.1:{server_info["port"]}/{begin}-{end}',timeout=5) as response:
                data = response.read(cdn.CHUNK+1)
            requests.append(len(data))
            return dict(totalsize=str(info['encrypted_bytes']).encode(),rangestart=str(begin).encode(),
                        filedata=data,rawfilemd5=info['md5'].encode())
        cdn._request = local_request
        def download(uid, *, should_continue=None):
            began=time.monotonic()
            cached = retained_download(core.media_cache_root, ACCOUNT, uid, 'image')
            if cached:
                path = cached[0]
            else:
                core.media_cache_root.mkdir(parents=True,exist_ok=True)
                with tempfile.TemporaryDirectory(dir=core.media_cache_root) as work:
                    path, verification = cdn._transfer(dict(aeskey=KEY.hex(),md5=info['md5'],hdlength=str(info['size'])),
                        'ab'*32,'image',{},Path(work),time.monotonic()+30,should_continue)
                    data = path.read_bytes()
                    path = retain_download(core.media_cache_root,ACCOUNT,uid,'image','png',data,
                        verified_sha256=hashlib.sha256(data).hexdigest(),verification=verification)
                transfers.append(dict(uid=uid,seconds=time.monotonic()-began,bytes=info['size'],finished_monotonic=time.monotonic()))
            return dict(messages=[dict(media=dict(status='ready',path=str(path)))])
        core.recheck_media = download
        samples_path = output/'rounds.jsonl'
        sampler_path = output/'process-samples.jsonl'
        stage = {'phase':'initialization','round':-1}
        def sample_loop():
            with sampler_path.open('w',buffering=1) as handle:
                while not stop.is_set():
                    try:
                        handle.write(json.dumps(dict(at_monotonic=time.monotonic(),**stage,**metrics()))+'\n')
                    except Exception as exc:
                        errors.append(type(exc).__name__)
                    stop.wait(.2)
        sampler = threading.Thread(target=sample_loop,daemon=True)
        sampler.start()
        init_start = time.monotonic()
        initial = core.refresh(trigger='manual')
        if not initial['ok']:
            raise RuntimeError('initial sync failed')
        initial_seconds = time.monotonic()-init_start
        # Following only alters this isolated index, never the installed account.
        with closing(sqlite3.connect(config['index_path'])) as con, con:
            for username,table in zip(CHATS,TABLES):
                con.execute('INSERT INTO conversation_selection(account,username,chat_table,state,source_dbs,selected_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                    (ACCOUNT,username,table,'following','[]','fixture','fixture'))
        from types import SimpleNamespace
        from noteford.media_worker import run_media_worker
        def media_loop():
            run_media_worker(lambda: [SimpleNamespace(id='fixture')], lambda _: core,
                             stop, media_wake)
        media_thread = threading.Thread(target=media_loop,daemon=True)
        media_thread.start()
        start = time.monotonic()+2
        atomic_json(root/'control.json',dict(start_monotonic=start,phase_seconds=args.phase_seconds))
        settings = dict(interval_seconds=10,start_monotonic=start,phase_seconds=args.phase_seconds,rounds_per_phase=args.phase_seconds//10,
            initial_seconds=initial_seconds,initial_rows=args.seed_rows,padding_mib=args.padding_mib,chat_count=len(CHATS),media_enabled=not args.no_media,
            source_bytes=json.loads((root/'producer-status.json').read_text())['source_bytes'],
            media_plain_bytes=info['size'],media_bandwidth_bytes_per_second=args.media_bandwidth_mib*1024*1024,
            media_burst=args.media_burst,pressure_media=args.pressure_media,
            limits='Synthetic WCDB pages/WAL and loopback media transport; excludes Electron UI, actual WeChat CDN/session acquisition and producer CPU/I/O.',
            pid=os.getpid(),producer_pid=producer.pid,server_pid=server.pid,python=sys.version)
        atomic_json(output/'experiment.json',settings)
        print(json.dumps(dict(event='started',**settings)),flush=True)
        total = 4*(args.phase_seconds//10)
        def backlog():
            with closing(sqlite3.connect(config['index_path'])) as con:
                indexed = con.execute('SELECT COUNT(*) FROM messages WHERE account=?',(ACCOUNT,)).fetchone()[0]
                media_total = con.execute('SELECT COUNT(*) FROM messages WHERE account=? AND local_type=3',(ACCOUNT,)).fetchone()[0]
            with closing(core.automatic_media._state()) as con:
                ready = con.execute("SELECT COUNT(*) FROM jobs WHERE account=? AND status='ready'",(ACCOUNT,)).fetchone()[0]
                failed = con.execute("SELECT COUNT(*) FROM jobs WHERE account=? AND status NOT IN('ready','running')",(ACCOUNT,)).fetchone()[0]
                jobs = con.execute('SELECT COUNT(*) FROM jobs WHERE account=?',(ACCOUNT,)).fetchone()[0]
            return dict(indexed=indexed,media_indexed=media_total,media_ready=ready,media_pending=media_total-ready,media_failed=failed,media_job_rows=jobs)
        with samples_path.open('w',buffering=1) as handle:
            for number in range(total+args.drain_rounds):
                due = start+number*10
                while time.monotonic() < due:
                    time.sleep(min(.2,due-time.monotonic()))
                phase = PHASES[min(3,number//(args.phase_seconds//10))] if number<total else 'drain'
                stage.update(phase=phase,round=number)
                before = metrics()
                begun = time.monotonic()
                error = None
                try:
                    result = core.refresh(trigger='interval')
                    ok = result.get('ok',False)
                except Exception as exc:
                    result = {}
                    ok = False
                    error = type(exc).__name__
                finished = time.monotonic()
                after = metrics()
                source = json.loads((root/'producer-status.json').read_text())
                queue = backlog()
                row = dict(round=number,phase=phase,elapsed_seconds=finished-start,duration_seconds=finished-begun,
                    scheduler_lag_seconds=begun-due,overran_interval=finished>due+10,ok=ok,error=error,
                    cpu_seconds=after['cpu_seconds']-before['cpu_seconds'],rss_bytes=after['rss_bytes'],footprint_bytes=after['footprint_bytes'],
                    read_bytes=after['read_bytes']-before['read_bytes'],write_bytes=after['write_bytes']-before['write_bytes'],
                    logical_write_bytes=after['logical_write_bytes']-before['logical_write_bytes'],
                    source_sent=source['sent'],text_pending=max(0,source['sent']-queue['indexed']),
                    threads=after['threads'],fds=after['fds'],network_bytes=sum(requests),
                    live_source=(result.get('result')or {}).get('live_source'),**queue)
                handle.write(json.dumps(row)+'\n')
                atomic_json(output/'latest.json',row)
                if number%6==0 or not ok:
                    print(json.dumps(row),flush=True)
                if phase=='drain' and producer.poll() is not None and row['text_pending']==0 and row['media_pending']==0:
                    break
        atomic_json(output/'final.json',dict(**backlog(),producer=json.loads((root/'producer-status.json').read_text()),
            helper_errors=errors,network_bytes=sum(requests),metrics=metrics(),elapsed_seconds=time.monotonic()-start))
        atomic_json(output/'transfers.json',transfers)
    finally:
        stop.set()
        media_wake.set()
        if media_thread is not None:
            media_thread.join(timeout=10)
        (root/'stop-producer').touch()
        for child in children:
            if child is producer:
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.terminate()
            else:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--mode',choices=['run','producer','server'],default='run')
    parser.add_argument('--phase-seconds',type=int,default=450)
    parser.add_argument('--padding-mib',type=int,default=128)
    parser.add_argument('--seed-rows',type=int,default=50000)
    parser.add_argument('--chat-count',type=int,default=10)
    parser.add_argument('--pressure-media',action='store_true',help='Repeat the media burst during the 1000-text/10s phase')
    parser.add_argument('--no-media',action='store_true',help='Text-only diagnostic, retaining background worker polling')
    parser.add_argument('--media-bandwidth-mib',type=float,default=2,help='Loopback source MiB/s; 0 means no source throttle')
    parser.add_argument('--media-burst',type=int,default=100)
    parser.add_argument('--image-size',type=int,default=512,help='Square RGB PNG fixture dimension')
    parser.add_argument('--drain-rounds',type=int,default=12)
    args=parser.parse_args()
    if args.chat_count<1 or args.seed_rows<args.chat_count or args.seed_rows%args.chat_count:
        parser.error('chat-count must be positive and divide seed-rows')
    if args.phase_seconds<10 or args.phase_seconds%10:
        parser.error('phase-seconds must be a positive multiple of 10')
    if args.media_bandwidth_mib<0 or args.media_burst<0 or not 1<=args.image_size<=4096:
        parser.error('invalid synthetic media settings')
    CHATS = [f'fixture{i}@chatroom' for i in range(args.chat_count)]
    TABLES = ['Msg_' + hashlib.md5(c.encode()).hexdigest() for c in CHATS]
    if args.no_media:
        PHASES[2]='continuous-text-repeat'
    if args.mode=='producer':
        produce(args.root,args.padding_mib,args.seed_rows,media_enabled=not args.no_media,media_burst=args.media_burst,pressure_media=args.pressure_media)
    elif args.mode=='server':
        serve(args.root,bandwidth_mib=args.media_bandwidth_mib,image_size=args.image_size)
    else:
        if args.output is None or args.phase_seconds%10:
            parser.error('--output required; phase-seconds must be a multiple of 10')
        run(args)
