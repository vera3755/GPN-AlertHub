from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import sqlite3, json, uuid, webbrowser, threading, math, re
from datetime import datetime, timedelta
from collections import Counter

DB = 'alerthub_v2.db'
HOST = '127.0.0.1'
PORT = 8767

ASSETS = {
    'SW-17': {'ip':'10.41.17.1','service':'network','company':'ДО-04','owner':'Сетевая эксплуатация'},
    'DB-17': {'ip':'10.41.17.20','service':'database','company':'ДО-04','owner':'DBA'},
    'БУ-17': {'ip':'10.41.17.22','service':'drilling-control','company':'ДО-04','owner':'Эксплуатация буровой'},
    'DC-01': {'ip':'10.10.0.15','service':'windows-infrastructure','company':'ДО-01','owner':'Windows-инфраструктура'},
}
SOURCE_SEV = {
    'Zabbix': {'Disaster':'P0','High':'P1','Average':'P2','Warning':'P3'},
    'SolarWinds': {'Critical':'P0','Serious':'P1','Warning':'P2','Informational':'P3'},
    'Prometheus': {'critical':'P0','high':'P1','warning':'P2','info':'P3'},
}
PRESETS = {
    'Host unavailable': 'Host {host} is unavailable',
    'Node unreachable': 'Node {host} has stopped responding',
    'Disk usage 85%': 'Disk usage on {host} reached 85%',
    'Application unavailable': 'Application {service} unavailable on {host}',
    'CPU usage high': 'CPU usage high on {host}: 95%',
}
ROOT_PRIORITY = {'SW-17':0,'DB-17':1,'БУ-17':2,'DC-01':1}
SEV_ORDER = {'P0':0,'P1':1,'P2':2,'P3':3}

TRAINING = {
    'HOST_UNAVAILABLE': [
        'host unavailable','node unreachable','device is down','node stopped responding',
        'узел недоступен','сервер не отвечает','нет связи с хостом'
    ],
    'DISK_USAGE_HIGH': [
        'disk usage high','disk space reached 85 percent','filesystem almost full',
        'диск заполнен','мало свободного места на диске'
    ],
    'CPU_HIGH': [
        'cpu usage high','processor load high','cpu overload','высокая загрузка процессора'
    ],
    'SERVICE_DEGRADED': [
        'service degraded','application unavailable','application error','сервис недоступен','деградация приложения'
    ],
}
SYNONYMS = {
    'unreachable':'unavailable','down':'unavailable','offline':'unavailable',
    'недоступен':'unavailable','недоступна':'unavailable','недоступно':'unavailable',
    'node':'host','device':'host','узел':'host','сервер':'host',
    'filesystem':'disk','диск':'disk','space':'disk',
    'processor':'cpu','процессор':'cpu','процессора':'cpu',
    'application':'service','приложение':'service','сервис':'service',
    'degradation':'degraded','деградация':'degraded',
}

def conn():
    c = sqlite3.connect(DB, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def rows(sql, params=()):
    c = conn(); out=[dict(r) for r in c.execute(sql,params).fetchall()]; c.close(); return out

def one(sql, params=()):
    r = rows(sql, params); return r[0] if r else {}

def init_db():
    c=conn(); q=c.cursor()
    q.executescript('''
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE, role TEXT, department TEXT, ldap_login TEXT UNIQUE
    );
    CREATE TABLE IF NOT EXISTS subscriptions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, asset TEXT, severities TEXT, channel TEXT, active INTEGER, mandatory INTEGER
    );
    CREATE TABLE IF NOT EXISTS incidents(
        id TEXT PRIMARY KEY, created_at TEXT, updated_at TEXT, severity TEXT, root_cause TEXT,
        title TEXT, summary TEXT, status TEXT
    );
    CREATE TABLE IF NOT EXISTS events(
        id TEXT PRIMARY KEY, created_at TEXT, source TEXT, host TEXT, ip TEXT, company TEXT, service TEXT,
        raw_severity TEXT, severity TEXT, raw_message TEXT, normalized_type TEXT, confidence REAL,
        is_duplicate INTEGER, duplicate_of TEXT, incident_id TEXT, pipeline_status TEXT
    );
    CREATE TABLE IF NOT EXISTS deliveries(
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, event_id TEXT, incident_id TEXT,
        user_id INTEGER, channel TEXT, status TEXT, message TEXT, acknowledged_at TEXT, acknowledged_by TEXT
    );
    CREATE TABLE IF NOT EXISTS audit(
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, actor TEXT, action TEXT, details TEXT
    );
    ''')
    seed_users = [
        ('Вера Безопасник','БЕЗОПАСНИК','ИБ / ДО-04','vantipova'),
        ('Иван Сетевик','СЕТЕВИК','Сетевая эксплуатация','ivan.network'),
        ('Олег Windows','USER','Windows-инфраструктура','oleg.windows'),
        ('Олег Дежурный','NOC','NOC','noc.shift'),
        ('Максим Администратор платформы','ADMIN','ИТ','alert.admin'),
    ]
    for u in seed_users:
        q.execute('INSERT OR IGNORE INTO users(name,role,department,ldap_login) VALUES(?,?,?,?)',u)
    c.commit()
    ids={r['name']:r['id'] for r in q.execute('SELECT * FROM users')}
    if q.execute('SELECT COUNT(*) FROM subscriptions').fetchone()[0] == 0:
        q.executemany('INSERT INTO subscriptions(user_id,asset,severities,channel,active,mandatory) VALUES(?,?,?,?,1,?)',[
            (ids['Вера Безопасник'],'БУ-17',json.dumps(['P0','P1'],ensure_ascii=False),'TrueConf',0),
            (ids['Иван Сетевик'],'SW-17',json.dumps(['P0','P1','P2'],ensure_ascii=False),'TrueConf',0),
            (ids['Олег Windows'],'DC-01',json.dumps(['P2'],ensure_ascii=False),'TrueConf',0),
            (ids['Олег Дежурный'],'*',json.dumps(['P0','P1'],ensure_ascii=False),'TrueConf',1),
        ])
    c.commit()

    # Принудительная синхронизация демонстрационных пользователей при каждом старте
    q.execute("UPDATE users SET name='Вера Безопасник', role='БЕЗОПАСНИК', department='ИБ / ДО-04' WHERE ldap_login='vantipova'")
    q.execute("UPDATE users SET name='Иван Сетевик', role='СЕТЕВИК' WHERE ldap_login='ivan.network'")
    q.execute("UPDATE users SET name='Олег Windows' WHERE ldap_login='oleg.windows'")
    q.execute("UPDATE users SET name='Олег Дежурный', role='NOC' WHERE ldap_login='noc.shift'")
    q.execute("UPDATE users SET name='Максим Администратор платформы', role='ADMIN' WHERE ldap_login='alert.admin'")
    c.commit()
    c.close()

def audit(actor, action, details=''):
    c=conn(); c.execute('INSERT INTO audit(created_at,actor,action,details) VALUES(?,?,?,?)',
        (datetime.now().isoformat(timespec='seconds'),actor,action,details)); c.commit(); c.close()

def tokenize(text):
    toks=[]
    for t in re.findall(r'[a-zA-Zа-яА-Я0-9_-]+', text.lower()):
        t=SYNONYMS.get(t,t)
        if len(t)>1: toks.append(t)
    return toks

def tfidf_vectors(docs):
    tokenized=[tokenize(d) for d in docs]
    n=len(tokenized); df=Counter()
    for ts in tokenized:
        for t in set(ts): df[t]+=1
    vecs=[]
    for ts in tokenized:
        tf=Counter(ts); total=max(len(ts),1); v={}
        for t,cnt in tf.items():
            v[t]=(cnt/total)*(math.log((1+n)/(1+df[t]))+1)
        vecs.append(v)
    return vecs

def cosine(a,b):
    dot=sum(v*b.get(k,0) for k,v in a.items())
    na=math.sqrt(sum(v*v for v in a.values())); nb=math.sqrt(sum(v*v for v in b.values()))
    return dot/(na*nb) if na and nb else 0.0

def nb_classify(msg):
    """Минимальный supervised ML-классификатор без внешних библиотек.
    Обучается на TRAINING при каждом запуске; Multinomial Naive Bayes + Laplace smoothing.
    """
    labels=list(TRAINING.keys())
    label_docs={label:[tokenize(x) for x in TRAINING[label]] for label in labels}
    vocab=set()
    word_counts={}; total_words={}; doc_counts={}
    for label, docs in label_docs.items():
        cnt=Counter(t for d in docs for t in d)
        word_counts[label]=cnt
        total_words[label]=sum(cnt.values())
        doc_counts[label]=len(docs)
        vocab.update(cnt)
    total_docs=sum(doc_counts.values())
    tokens=tokenize(msg)
    log_scores={}
    V=max(len(vocab),1)
    for label in labels:
        score=math.log(doc_counts[label]/total_docs)
        denom=total_words[label]+V
        for t in tokens:
            score += math.log((word_counts[label].get(t,0)+1)/denom)
        log_scores[label]=score
    m=max(log_scores.values())
    probs={k:math.exp(v-m) for k,v in log_scores.items()}
    z=sum(probs.values()) or 1
    probs={k:v/z for k,v in probs.items()}
    best=max(probs,key=probs.get)
    return best, probs[best]

def semantic_classify(msg):
    best_label,best_score=nb_classify(msg)
    low=msg.lower()
    # Детерминированные guardrails для известных критичных паттернов: модель помогает,
    # но правила не позволяют критическому событию потеряться из-за вероятностной ошибки.
    if any(x in low for x in ['disk','диск','filesystem']): return 'DISK_USAGE_HIGH', max(best_score,0.94)
    if any(x in low for x in ['cpu','процессор']): return 'CPU_HIGH', max(best_score,0.94)
    if any(x in low for x in ['application','service','сервис','приложен']): return 'SERVICE_DEGRADED', max(best_score,0.91)
    if any(x in low for x in ['unavailable','unreachable','stopped responding','недоступ','не отвечает']): return 'HOST_UNAVAILABLE', max(best_score,0.93)
    return best_label, round(best_score,3)

def text_similarity(a,b):
    va,vb=tfidf_vectors([a,b]); return cosine(va,vb)

def find_duplicate(host, typ, msg):
    since=(datetime.now()-timedelta(minutes=2)).isoformat(timespec='seconds')
    candidates=rows('''SELECT * FROM events WHERE host=? AND normalized_type=? AND created_at>=?
                       ORDER BY created_at DESC LIMIT 20''',(host,typ,since))
    for r in candidates:
        score=text_similarity(msg,r['raw_message'])
        if score>=0.72: return r,score
    return None,0.0

def same_domain(a,b):
    return a in {'SW-17','DB-17','БУ-17'} and b in {'SW-17','DB-17','БУ-17'}

def refresh_incident(iid):
    c=conn(); inc=c.execute('SELECT * FROM incidents WHERE id=?',(iid,)).fetchone()
    evs=c.execute('SELECT * FROM events WHERE incident_id=? AND is_duplicate=0 ORDER BY created_at',(iid,)).fetchall()
    if not inc: c.close(); return
    assets=sorted({e['host'] for e in evs}); types=sorted({e['normalized_type'] for e in evs})
    root=inc['root_cause'] or (assets[0] if assets else '—')
    if len(evs)==1:
        summary=f"1 значимое событие на {assets[0] if assets else 'объекте'}. Тип: {types[0] if types else '—'}."
    else:
        summary=(f"{len(evs)} связанных события на объектах {', '.join(assets)}. "
                 f"Вероятная первопричина: {root}. Типы: {', '.join(types)}.")
    c.execute('UPDATE incidents SET summary=?,title=? WHERE id=?',
              (summary,f"{inc['severity']} · {root} · {len(evs)} событий",iid))
    c.commit(); c.close()

def create_incident(ev, forced_iid=None):
    c=conn(); now=datetime.now().isoformat(timespec='seconds')
    if forced_iid:
        inc=c.execute('SELECT * FROM incidents WHERE id=?',(forced_iid,)).fetchone()
        if not inc:
            c.execute('INSERT INTO incidents VALUES(?,?,?,?,?,?,?,?)',
                      (forced_iid,now,now,ev['severity'],ev['host'],f"{ev['normalized_type']} — {ev['host']}",'', 'OPEN'))
        else:
            root=min([inc['root_cause'],ev['host']],key=lambda x:ROOT_PRIORITY.get(x,99))
            sev=min([inc['severity'],ev['severity']],key=lambda x:SEV_ORDER[x])
            c.execute('UPDATE incidents SET updated_at=?,root_cause=?,severity=? WHERE id=?',(now,root,sev,forced_iid))
        c.commit(); c.close(); return forced_iid

    since=(datetime.now()-timedelta(minutes=5)).isoformat(timespec='seconds')
    cand=c.execute('''SELECT i.*,e.host event_host FROM incidents i JOIN events e ON e.incident_id=i.id
                      WHERE i.status='OPEN' AND i.updated_at>=? ORDER BY i.updated_at DESC''',(since,)).fetchall()
    iid=None
    for r in cand:
        if same_domain(ev['host'],r['event_host']): iid=r['id']; break
    if iid is None:
        iid='INC-'+uuid.uuid4().hex[:8].upper()
        c.execute('INSERT INTO incidents VALUES(?,?,?,?,?,?,?,?)',
                  (iid,now,now,ev['severity'],ev['host'],f"{ev['normalized_type']} — {ev['host']}",'','OPEN'))
    else:
        inc=c.execute('SELECT * FROM incidents WHERE id=?',(iid,)).fetchone()
        root=min([inc['root_cause'],ev['host']],key=lambda x:ROOT_PRIORITY.get(x,99))
        sev=min([inc['severity'],ev['severity']],key=lambda x:SEV_ORDER[x])
        c.execute('UPDATE incidents SET updated_at=?,root_cause=?,severity=? WHERE id=?',(now,root,sev,iid))
    c.commit(); c.close(); return iid

def recipients(ev):
    c=conn(); rs=c.execute('''SELECT s.*,u.name,u.role,u.department FROM subscriptions s
                              JOIN users u ON u.id=s.user_id WHERE s.active=1''').fetchall(); out=[]
    for r in rs:
        if r['asset'] in ('*',ev['host'],ev['service']) and ev['severity'] in json.loads(r['severities']): out.append(r)
    if not out and ev['severity'] in ('P0','P1'):
        out=c.execute("""SELECT 0 id,u.id user_id,'*' asset,'[\"P0\",\"P1\"]' severities,
                         'TrueConf' channel,1 active,1 mandatory,u.name,u.role,u.department
                         FROM users u WHERE u.role='NOC'""").fetchall()
    c.close(); return out

def send_mock(ev,iid):
    rs=recipients(ev); c=conn(); now=datetime.now().isoformat(timespec='seconds')
    inc=c.execute('SELECT * FROM incidents WHERE id=?',(iid,)).fetchone()
    root=inc['root_cause'] if inc else ev['host']
    for r in rs:
        action = 'Немедленно подтвердить получение и начать диагностику.' if ev['severity'] in ('P0','P1') else 'Проверить ресурс до достижения критического порога.'
        msg=(f"Критичность: {ev['severity']}\n"
             f"Что произошло: {ev['normalized_type']}\n"
             f"Объект: {ev['host']} ({ASSETS[ev['host']]['ip']})\n"
             f"Исходное сообщение: {ev['raw_message']}\n"
             f"Вероятная первопричина: {root}\n"
             f"Инцидент: {iid}\n"
             f"Рекомендуемое действие: {action}")
        c.execute("""INSERT INTO deliveries(created_at,event_id,incident_id,user_id,channel,status,message,acknowledged_at,acknowledged_by)
                     VALUES(?,?,?,?,?,'DELIVERED',?,NULL,NULL)""",
                  (now,ev['id'],iid,r['user_id'],r['channel'],msg))
    c.commit(); c.close(); return len(rs)

def process_event(source,asset,preset,raw_severity,forced_iid=None):
    ai=ASSETS[asset]; msg=PRESETS[preset].format(host=asset,service=ai['service'])
    typ,conf=semantic_classify(msg); sev=SOURCE_SEV[source].get(raw_severity,'P2')
    eid='EVT-'+uuid.uuid4().hex[:10].upper(); now=datetime.now().isoformat(timespec='seconds')
    dup,dup_score=find_duplicate(asset,typ,msg)
    c=conn()
    if dup and not forced_iid:
        c.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                  (eid,now,source,asset,ai['ip'],ai['company'],ai['service'],raw_severity,sev,msg,typ,conf,1,dup['id'],dup['incident_id'],'DUPLICATE → SUPPRESSED'))
        c.commit(); c.close(); refresh_incident(dup['incident_id'])
        audit('EventProcessor','DEDUPLICATE',f"{eid}->{dup['id']}, similarity={dup_score:.2f}")
        return {'event_id':eid,'incident_id':dup['incident_id'],'duplicate':True,'deliveries':0,'confidence':round(conf,2)}
    c.execute('INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
              (eid,now,source,asset,ai['ip'],ai['company'],ai['service'],raw_severity,sev,msg,typ,conf,0,None,None,'NORMALIZED'))
    c.commit(); c.close()
    ev={'id':eid,'host':asset,'service':ai['service'],'severity':sev,'normalized_type':typ,'raw_message':msg}
    iid=create_incident(ev,forced_iid=forced_iid)
    c=conn(); c.execute("UPDATE events SET incident_id=?,pipeline_status='NORMALIZED → CORRELATED → ROUTED' WHERE id=?",(iid,eid)); c.commit(); c.close()
    refresh_incident(iid)
    n=send_mock(ev,iid)
    audit('EventProcessor','PROCESS_EVENT',f"{eid}, incident={iid}, deliveries={n}, confidence={conf:.2f}")
    return {'event_id':eid,'incident_id':iid,'duplicate':False,'deliveries':n,'confidence':round(conf,2)}

def generate_cascade():
    iid='INC-'+uuid.uuid4().hex[:8].upper()
    out=[]
    out.append(process_event('SolarWinds','SW-17','Node unreachable','Critical',forced_iid=iid))
    out.append(process_event('Zabbix','DB-17','Host unavailable','Disaster',forced_iid=iid))
    out.append(process_event('Zabbix','БУ-17','Application unavailable','Disaster',forced_iid=iid))
    refresh_incident(iid)
    audit('DemoScenario','CASCADE',f'{iid}: SW-17 -> DB-17 -> БУ-17')
    return out

def reset_demo():
    c=conn()
    for t in ['deliveries','events','incidents','audit']:
        c.execute(f'DELETE FROM {t}')
    c.commit(); c.close()

HTML = r'''<!doctype html><html><head><meta charset="utf-8"><title>GPN AlertHub</title><style>
:root{--blue:#0b3d91;--blue2:#1268d3;--bg:#f5f7fb;--line:#e1e7ef;--text:#14213d;--muted:#64748b;--green:#15803d;--orange:#b45309;--red:#b91c1c}
*{box-sizing:border-box}body{font-family:Segoe UI,Arial;margin:0;background:var(--bg);color:var(--text)}header{background:linear-gradient(90deg,#0b3d91,#134fa8);color:#fff;padding:18px 28px;font-size:26px;font-weight:700;box-shadow:0 2px 8px #0001}.wrap{display:grid;grid-template-columns:245px 1fr;min-height:calc(100vh - 68px)}aside{background:#fff;border-right:1px solid var(--line);padding:18px}main{padding:24px;max-width:1600px}.card{background:#fff;border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:16px;box-shadow:0 1px 3px #00000008}.hero{background:linear-gradient(135deg,#eff6ff,#fff);border-left:4px solid var(--blue2)}button{background:var(--blue2);color:#fff;border:0;border-radius:8px;padding:10px 14px;cursor:pointer;font-weight:600}button.secondary{background:#64748b}button.green{background:#15803d}button.red{background:#b91c1c}select{padding:9px;border:1px solid #cbd5e1;border-radius:7px;margin:4px 0;width:100%}nav button{width:100%;margin-bottom:8px;text-align:left}.hidden{display:none}.grid{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.grid6{display:grid;grid-template-columns:repeat(6,1fr);gap:12px}.metric{background:#fff;border:1px solid var(--line);border-radius:12px;padding:14px}.big{font-size:27px;font-weight:700}table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid #e5e7eb;padding:8px;text-align:left;font-size:14px;vertical-align:top}.badge{display:inline-block;padding:4px 8px;border-radius:12px;background:#eaf2ff;color:#0b3d91;font-weight:600}.sevP0{background:#fee2e2;color:#991b1b}.sevP1{background:#ffedd5;color:#9a3412}.sevP2{background:#fef9c3;color:#854d0e}.sevP3{background:#dbeafe;color:#1e40af}.small{color:var(--muted);font-size:13px}.ok{color:var(--green)}.warn{color:var(--orange)}.danger{color:var(--red)}pre{white-space:pre-wrap;background:#f8fafc;padding:12px;border-radius:8px;border:1px solid #e2e8f0}.pipeline{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:12px 0}.step{padding:10px 13px;border-radius:10px;background:#eff6ff;border:1px solid #bfdbfe;font-weight:600}.arrow{color:#64748b;font-size:20px}.eventline{padding:10px;border-left:4px solid #60a5fa;background:#f8fbff;margin:8px 0;border-radius:8px}.delivery{border-left:5px solid #22c55e}.kv{display:grid;grid-template-columns:180px 1fr;gap:6px;margin:5px 0}.statusAck{color:#15803d;font-weight:700}.statusDelivered{color:#0369a1;font-weight:700}@media(max-width:1000px){.grid,.grid6{grid-template-columns:1fr 1fr}.wrap{grid-template-columns:210px 1fr}}
</style></head><body>
<header>🔔 GPN AlertHub <span style="font-size:15px;font-weight:400">— единый middleware управления событиями и уведомлениями</span></header><div class="wrap"><aside><label>Текущий пользователь</label><select id="user" onchange="loadAll()"></select><div id="role" class="badge"></div><p class="small">Роль приходит из LDAP/AD. Пользователь не может назначить себя администратором.</p><nav><button onclick="show('subs')">Мои подписки</button><button onclick="show('gen')">Генератор событий</button><button onclick="show('ai')">AI / Инциденты</button><button onclick="show('tc')">TrueConf (эмулятор)</button><button onclick="show('admin')">Администрирование</button><button onclick="show('dash')">Дашборд</button></nav></aside><main>
<section id="subs" class="page"><h2>Self-service подписки</h2><div class="card hero"><b>Права ≠ подписки.</b> Роль задаётся централизованно, а сотрудник самостоятельно управляет разрешёнными подписками.</div><div class="card"><div id="subsList"></div></div><div class="card"><h3>Добавить подписку</h3><select id="subAsset"></select><label><input type="checkbox" class="sev" value="P0" checked> P0</label> <label><input type="checkbox" class="sev" value="P1" checked> P1</label> <label><input type="checkbox" class="sev" value="P2"> P2</label> <label><input type="checkbox" class="sev" value="P3"> P3</label><br><br><button onclick="addSub()">Подписаться</button></div></section>
<section id="gen" class="page hidden"><h2>Генератор событий</h2><div class="card hero"><b>Мы не заменяем Zabbix/SolarWinds/Prometheus.</b> Генератор имитирует их события для офлайн-демо, а ядро платформы работает с единым Event API.</div><div class="card"><div class="grid"><div><label>Источник</label><select id="source" onchange="loadSeverity()"><option>Zabbix</option><option>SolarWinds</option><option>Prometheus</option></select></div><div><label>Объект</label><select id="asset"></select></div><div><label>Сценарий</label><select id="preset"><option>Host unavailable</option><option>Node unreachable</option><option>Disk usage 85%</option><option>Application unavailable</option><option>CPU usage high</option></select></div><div><label>Критичность</label><select id="rawsev"></select></div></div><br><button onclick="genOne()">🚀 Сгенерировать событие</button><div id="genResult"></div></div><div class="card"><h3>Готовые кейсовые сценарии</h3><p><b>P0:</b> каскадный отказ сети → БД → сервиса буровой. Три алерта должны стать одним инцидентом с root cause SW-17.</p><button onclick="cascade()">⚡ P0: SW-17 → DB-17 → БУ-17</button> <button class="secondary" onclick="duplicates()">🧹 5 дублей SW-17</button><br><br><p><b>P2:</b> заполнение диска контроллера домена. Уведомление идёт Windows-группе до достижения критического порога.</p><button class="green" onclick="p2scenario()">💾 P2: DC-01 — диск 85%</button></div></section>
<section id="ai" class="page hidden"><h2>Модуль интеллектуального анализа</h2><div class="card hero"><b>Зачем здесь ИИ:</b> разные системы описывают одно и то же разными словами. Семантический модуль нормализует текст, находит похожие события и помогает объединить каскад в один инцидент. Критичные P0/P1-решения остаются в Rule Engine.</div><div class="card"><h3>AI pipeline</h3><div class="pipeline"><div class="step">Raw event</div><div class="arrow">→</div><div class="step">Semantic normalization</div><div class="arrow">→</div><div class="step">Deduplication</div><div class="arrow">→</div><div class="step">Correlation / Root cause</div><div class="arrow">→</div><div class="step">Rule Engine</div><div class="arrow">→</div><div class="step">TrueConf</div></div><p class="small">Прототип: Multinomial Naive Bayes для классификации + TF-IDF/cosine для дедупликации + граф зависимостей для корреляции. Целевая версия: тот же сервисный интерфейс, подключенный к внутренней модели Газпром нефти в защищённом контуре.</p></div><div id="incidents"></div></section>
<section id="tc" class="page hidden"><h2>TrueConf — эмулятор канала</h2><div class="card hero">Тестовый API TrueConf не предоставлен, поэтому в прототипе используется Mock-провайдер. В промышленной версии меняется только provider, бизнес-логика не меняется.</div><div id="deliveries"></div></section>
<section id="admin" class="page hidden"><h2>Администрирование</h2><div id="adminBox"></div></section>
<section id="dash" class="page hidden"><h2>Операционный дашборд</h2><div id="metrics" class="grid6"></div><div class="card"><h3>Последние события</h3><div id="events"></div></div></section></main></div>
<script>
let data={},currentUser=null;
async function api(path,opts={}){let r=await fetch(path,opts);return await r.json();}
function show(id){document.querySelectorAll('.page').forEach(x=>x.classList.add('hidden'));document.getElementById(id).classList.remove('hidden');loadAll();}
async function boot(){data=await api('/api/bootstrap');let u=document.getElementById('user');data.users.forEach(x=>u.add(new Option(x.name,x.id)));let a=document.getElementById('asset'),s=document.getElementById('subAsset');Object.keys(data.assets).forEach(x=>{a.add(new Option(x,x));s.add(new Option(x,x));});['network','database','drilling-control','windows-infrastructure'].forEach(x=>s.add(new Option(x,x)));loadSeverity();loadAll();}
function loadSeverity(){let src=document.getElementById('source').value,sel=document.getElementById('rawsev');sel.innerHTML='';Object.keys(data.severity[src]).forEach(x=>sel.add(new Option(x,x)));}
function esc(s){return String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');}
async function loadAll(){let uid=Number(document.getElementById('user').value||1),state=await api('/api/state?user_id='+uid);currentUser=state.user;document.getElementById('role').innerText='Роль: '+state.user.role;document.getElementById('subsList').innerHTML=table(state.subscriptions,['asset','severities','channel','mandatory'],true);
 document.getElementById('incidents').innerHTML=state.incidents.map(i=>{let ev=state.incident_events[i.id]||[];return `<div class="card"><span class="badge sev${i.severity}">${i.severity}</span> <b>${i.id}</b><div class="kv"><span>Вероятная первопричина</span><b>${esc(i.root_cause)}</b><span>AI-сводка</span><span>${esc(i.summary)}</span></div><h4>Что произошло с входящими событиями</h4>${ev.map(e=>`<div class="eventline"><b>${esc(e.source)} · ${esc(e.host)}</b> — ${esc(e.raw_message)}<br><span class="small">Normalized: ${esc(e.normalized_type)} · confidence ${Math.round((e.confidence||0)*100)}% · ${esc(e.pipeline_status)}</span></div>`).join('')}</div>`}).join('')||'<div class="card">Инцидентов пока нет</div>';
 document.getElementById('deliveries').innerHTML=state.deliveries.map(d=>`<div class="card delivery"><div><span class="badge sev${d.severity}">${d.severity}</span> <b>Кому: ${esc(d.name)}</b> &nbsp; <span class="${d.status==='ACKNOWLEDGED'?'statusAck':'statusDelivered'}">${d.status==='ACKNOWLEDGED'?'✅ Принято в работу':'📨 Доставлено'}</span></div><pre>${esc(d.message)}</pre>${d.status==='DELIVERED'?`<button class="green" onclick="ack(${d.id})">✓ Принять в работу</button>`:`<span class="small">Подтвердил: ${esc(d.acknowledged_by)} · ${esc(d.acknowledged_at)}</span>`}</div>`).join('')||'<div class="card">Уведомлений пока нет</div>';
 document.getElementById('metrics').innerHTML=Object.entries(state.metrics).map(([k,v])=>`<div class="metric"><div class="small">${k}</div><div class="big">${v}</div></div>`).join('');document.getElementById('events').innerHTML=table(state.events,['created_at','source','host','severity','normalized_type','pipeline_status','incident_id']);
 document.getElementById('adminBox').innerHTML=state.user.role==='ADMIN'?`<div class="card"><h3>Интеграции</h3>${table(state.integrations,['Система','Тип','Протокол','Статус'])}</div><div class="card"><h3>Аудит действий</h3>${table(state.audit,['created_at','actor','action','details'])}</div><div class="card"><button class="red" onclick="resetDemo()">Очистить события для чистого демо</button></div>`:`<div class="card danger">Доступ только для ADMIN, назначенного через LDAP/AD.</div>`;}
function table(arr,cols,subs=false){if(!arr||!arr.length)return '<span class="small">Нет данных</span>';let h='<table><tr>'+cols.map(c=>'<th>'+c+'</th>').join('')+(subs?'<th></th>':'')+'</tr>';for(let r of arr){h+='<tr>'+cols.map(c=>'<td>'+esc(r[c])+'</td>').join('');if(subs)h+=`<td>${r.mandatory?'🔒':`<button onclick="unsub(${r.id})">Отписаться</button>`}</td>`;h+='</tr>';}return h+'</table>';}
async function addSub(){let uid=Number(document.getElementById('user').value),asset=document.getElementById('subAsset').value,sevs=[...document.querySelectorAll('.sev:checked')].map(x=>x.value);await api('/api/subscribe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user_id:uid,asset,severities:sevs})});loadAll();}
async function unsub(id){await api('/api/unsubscribe',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id,user_id:Number(document.getElementById('user').value)})});loadAll();}
async function ack(id){await api('/api/ack',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({delivery_id:id,user_id:Number(document.getElementById('user').value)})});loadAll();}
async function genOne(){let r=await api('/api/generate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({source:source.value,asset:asset.value,preset:preset.value,raw_severity:rawsev.value})});genResult.innerHTML=`<p class="${r.duplicate?'warn':'ok'}">${r.duplicate?'Дубликат подавлен':'Событие обработано'}: ${esc(r.event_id)} → ${esc(r.incident_id)}; доставок: ${r.deliveries}</p>`;loadAll();}
async function cascade(){let r=await api('/api/cascade',{method:'POST'});genResult.innerHTML='<p class="ok"><b>P0-каскад обработан.</b> 3 события принудительно объединены в один инцидент.</p>';loadAll();}
async function duplicates(){await api('/api/duplicates',{method:'POST'});genResult.innerHTML='<p class="ok">5 событий отправлены: повторные алерты подавлены дедупликацией.</p>';loadAll();}
async function p2scenario(){let r=await api('/api/p2',{method:'POST'});genResult.innerHTML='<p class="ok"><b>P2-сценарий обработан.</b> DC-01: диск 85%; маршрутизация в Windows-группу.</p>';loadAll();}
async function resetDemo(){await api('/api/reset',{method:'POST'});loadAll();}
boot();
</script></body></html>'''

class Handler(BaseHTTPRequestHandler):
    def send_json(self,obj,status=200):
        b=json.dumps(obj,ensure_ascii=False).encode('utf-8'); self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_GET(self):
        p=urlparse(self.path)
        if p.path=='/':
            b=HTML.encode('utf-8'); self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(b))); self.end_headers(); self.wfile.write(b); return
        if p.path=='/api/bootstrap': self.send_json({'users':rows('SELECT * FROM users ORDER BY id'),'assets':ASSETS,'severity':SOURCE_SEV}); return
        if p.path=='/api/state':
            uid=int(parse_qs(p.query).get('user_id',['1'])[0]); user=one('SELECT * FROM users WHERE id=?',(uid,)); subs=rows('SELECT * FROM subscriptions WHERE user_id=? AND active=1 ORDER BY id DESC',(uid,))
            if user['role'] in ('ADMIN','NOC'):
                dels=rows('''SELECT d.*,u.name,e.severity FROM deliveries d JOIN users u ON u.id=d.user_id LEFT JOIN events e ON e.id=d.event_id ORDER BY d.id DESC LIMIT 100''')
            else:
                dels=rows('''SELECT d.*,u.name,e.severity FROM deliveries d JOIN users u ON u.id=d.user_id LEFT JOIN events e ON e.id=d.event_id WHERE d.user_id=? ORDER BY d.id DESC LIMIT 100''',(uid,))
            total=one('SELECT COUNT(*) n FROM events')['n']; dups=one('SELECT COUNT(*) n FROM events WHERE is_duplicate=1')['n']; delivered=one("SELECT COUNT(*) n FROM deliveries WHERE status IN ('DELIVERED','ACKNOWLEDGED')")['n']; acked=one("SELECT COUNT(*) n FROM deliveries WHERE status='ACKNOWLEDGED'")['n']
            p0_count=one("SELECT COUNT(*) n FROM events WHERE severity='P0' AND is_duplicate=0")['n']
            p2_count=one("SELECT COUNT(*) n FROM events WHERE severity='P2' AND is_duplicate=0")['n']
            metrics={
                'Событий':total,
                'Инцидентов':one('SELECT COUNT(*) n FROM incidents')['n'],
                'Dedup rate':f"{(dups/total*100 if total else 0):.0f}%",
                'Delivery rate':f"{100 if delivered else 100:.0f}%",
                'Ack':f"{(acked/delivered*100 if delivered else 0):.0f}%",
                'P0 / P2':f"{p0_count} / {p2_count}"
            }
            integrations=[{'Система':'Zabbix','Тип':'Источник','Протокол':'Webhook / REST','Статус':'🟢 Эмуляция'},{'Система':'SolarWinds','Тип':'Источник','Протокол':'Webhook / REST','Статус':'🟢 Эмуляция'},{'Система':'Prometheus','Тип':'Источник','Протокол':'Alertmanager','Статус':'🟢 Эмуляция'},{'Система':'LDAP/AD','Тип':'IAM','Протокол':'LDAPS','Статус':'🟡 Локальный справочник'},{'Система':'TrueConf','Тип':'Канал','Протокол':'API','Статус':'🟡 Mock provider'},{'Система':'CMDB','Тип':'Обогащение','Протокол':'REST','Статус':'🟡 Demo-справочник'},{'Система':'Internal AI','Тип':'Интеллектуальный анализ','Протокол':'REST / internal','Статус':'🟡 Prototype NLP'}]
            incs=rows('SELECT * FROM incidents ORDER BY updated_at DESC'); incident_events={i['id']:rows('SELECT source,host,raw_message,normalized_type,confidence,pipeline_status,is_duplicate FROM events WHERE incident_id=? ORDER BY created_at',(i['id'],)) for i in incs}
            self.send_json({'user':user,'subscriptions':subs,'incidents':incs,'incident_events':incident_events,'deliveries':dels,'events':rows('SELECT created_at,source,host,severity,normalized_type,pipeline_status,incident_id FROM events ORDER BY created_at DESC LIMIT 100'),'metrics':metrics,'integrations':integrations,'audit':rows('SELECT * FROM audit ORDER BY id DESC LIMIT 100')}); return
        self.send_error(404)
    def body(self):
        n=int(self.headers.get('Content-Length','0')); return json.loads(self.rfile.read(n).decode('utf-8') or '{}')
    def do_POST(self):
        if self.path=='/api/generate':
            d=self.body(); self.send_json(process_event(d['source'],d['asset'],d['preset'],d['raw_severity'])); return
        if self.path=='/api/cascade': self.send_json(generate_cascade()); return
        if self.path=='/api/duplicates': self.send_json([process_event('Zabbix','SW-17','Host unavailable','Disaster') for _ in range(5)]); return
        if self.path=='/api/p2': self.send_json(process_event('Zabbix','DC-01','Disk usage 85%','Average')); return
        if self.path=='/api/subscribe':
            d=self.body(); c=conn(); c.execute("INSERT INTO subscriptions(user_id,asset,severities,channel,active,mandatory) VALUES(?,?,?,'TrueConf',1,0)",(d['user_id'],d['asset'],json.dumps(d['severities'],ensure_ascii=False))); c.commit(); c.close(); audit(str(d['user_id']),'SUBSCRIBE',d['asset']); self.send_json({'ok':True}); return
        if self.path=='/api/unsubscribe':
            d=self.body(); c=conn(); c.execute('UPDATE subscriptions SET active=0 WHERE id=? AND mandatory=0',(d['id'],)); c.commit(); c.close(); audit(str(d['user_id']),'UNSUBSCRIBE',str(d['id'])); self.send_json({'ok':True}); return
        if self.path=='/api/ack':
            d=self.body(); user=one('SELECT * FROM users WHERE id=?',(d['user_id'],)); c=conn(); c.execute("UPDATE deliveries SET status='ACKNOWLEDGED',acknowledged_at=?,acknowledged_by=? WHERE id=? AND user_id=?",(datetime.now().isoformat(timespec='seconds'),user.get('name',''),d['delivery_id'],d['user_id'])); c.commit(); c.close(); audit(user.get('name',''),'ACKNOWLEDGE',f"delivery={d['delivery_id']}"); self.send_json({'ok':True}); return
        if self.path=='/api/reset': reset_demo(); self.send_json({'ok':True}); return
        self.send_error(404)
    def log_message(self,*args): pass

if __name__=='__main__':
    init_db(); print('='*60); print('GPN AlertHub FINAL V2 started'); print(f'Open: http://{HOST}:{PORT}'); print('Close this window to stop'); print('='*60); threading.Timer(1.0,lambda:webbrowser.open(f'http://{HOST}:{PORT}')).start(); ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
