"""数据库访问模块。

职责:
- FlightLog(上线日志): 机组/管制上线写一行, 下线填 offlineAt
- FlightPlan(飞行计划): 机组 $FP 落库; 管制员上线时读取并广播

说明: fsd-py 主要依赖标准库, pymysql 为可选依赖
(证书加载已使用其从 MySQL 读取, 见 certificate.load_from_db)。
"""

import logging
import threading

logger = logging.getLogger("fsd.db")

_db_config = {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "root",
    "password": "",
    "name": "cwfly",
}


def init_db(config):
    """从 fsd.conf [database] 段初始化连接配置。"""
    _db_config["host"] = config.get("database", "host", "127.0.0.1")
    _db_config["port"] = int(config.get("database", "port", 3306))
    _db_config["user"] = config.get("database", "user", "root")
    _db_config["password"] = config.get("database", "password", "")
    _db_config["name"] = config.get("database", "name", "cwfly")


# 每线程复用一条连接（线程池里的 worker 会长期复用，避免每次 upsert 都新建连接）
_local = threading.local()


def _connect():
    import pymysql
    return pymysql.connect(
        host=_db_config["host"],
        port=_db_config["port"],
        user=_db_config["user"],
        password=_db_config["password"],
        database=_db_config["name"],
        charset="utf8mb4",
        connect_timeout=3,
        read_timeout=5,
        write_timeout=5,
    )


def _conn():
    """返回本线程复用的 MySQL 连接（连接失效时自动重连）。

    修复(2026-10-16)：此前每次调用都新建连接 —— 机组位置是 2 秒一次 upsert，
    管制位置同样节流 2 秒，于是连接被反复创建/销毁（曾观察到 Threads_connected=41）。
    现在按线程缓存；取用前 ping(reconnect=True) 并在取用时清理可能残留的事务，
    避免上个操作失败后未提交的事务污染下一个操作。
    调用方原有的 conn.close() 调用点已统一替换为 _release()（复用时无需真正关闭）。
    """
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.ping(reconnect=True)
            # 清理可能残留的未提交事务（上个操作中途异常时）
            try:
                conn.rollback()
            except Exception:
                pass
            return conn
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            _local.conn = None
    conn = _connect()
    _local.conn = conn
    return conn


def _release(conn):
    """归还连接：线程内复用时不必真正关闭（保留原先 conn.close() 调用点的语义）。"""
    return


def write_online(user_id, callsign, ip):
    """机组/管制上线: 写一行 FlightLog, 返回新行 id(用于下线更新 offlineAt)。

    user_id: 平台用户ID(字符串, 按列长截断)
    callsign: 呼号(≤7)
    ip: 客户端 IP(≤16)
    """
    if not callsign:
        return None
    try:
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO `FlightLog` (userId, callsign, createdIp, createdAt) "
                "VALUES (%s, %s, %s, NOW())",
                (str(user_id or "")[:4], str(callsign)[:7], str(ip or "")[:16]),
            )
            row_id = cur.lastrowid
        conn.commit()
        _release(conn)
        return row_id
    except Exception as e:
        logger.warning(f"FlightLog online insert failed: {e}")
        return None


def update_offline(log_id):
    """下线: 更新 FlightLog.offlineAt。"""
    if not log_id:
        return
    try:
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE `FlightLog` SET offlineAt=NOW() WHERE id=%s", (log_id,))
        conn.commit()
        _release(conn)
    except Exception as e:
        logger.warning(f"FlightLog offline update failed: {e}")


def get_latest_user_id(callsign):
    """按 callsign 匹配 FlightLog, 取 createdAt 最新一行的 userId。

    用于机组 $FP 到达时反查真实平台用户ID并填入 FlightPlan。
    """
    if not callsign:
        return None
    try:
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT userId FROM `FlightLog` WHERE callsign=%s "
                "ORDER BY createdAt DESC LIMIT 1",
                (callsign,),
            )
            row = cur.fetchone()
        _release(conn)
        return row[0] if row else None
    except Exception as e:
        logger.warning(f"FlightLog query failed: {e}")
        return None


def write_flight_plan(user_id, callsign, fp):
    """机组 $FP 落库到 FlightPlan(FlightPlan 各字段 NOT NULL, 全部显式写入)。

    30 分钟内重复提交时覆盖该用户最近一条计划(与官网提交一致), 保留原 shareId。
    fp: 从 $FP 解析出的 FlightPlan 对象(含 type/aircraft/tascruise/depairport/...)。
    """
    if not fp or not callsign:
        return False
    try:
        import datetime
        now_dt = datetime.datetime.now()
        now = now_dt.strftime('%Y/%m/%d %H:%M:%S')
        data = (
            str(user_id or "")[:4],
            str(callsign)[:191],
            'IFR' if str(getattr(fp, 'type', 'I'))[:1].upper() == 'I' else 'VFR',
            str(getattr(fp, 'aircraft', ''))[:191],
            "",  # wakeCategory
            "",  # equipment
            "",  # transponder
            str(getattr(fp, 'depairport', ''))[:191],
            str(getattr(fp, 'deptime', '') or '')[:191],
            str(getattr(fp, 'altitude', ''))[:191],
            str(getattr(fp, 'tascruise', '') or '')[:191],
            str(getattr(fp, 'destairport', ''))[:191],
            str(getattr(fp, 'altairport', ''))[:191],
            "",  # flightTime
            "",  # endurance
            str(getattr(fp, 'route', ''))[:191],
            str(getattr(fp, 'remarks', ''))[:191],
            "",  # shareId
            now,  # filedAt
            1,    # syncFsd: 机组 $FP 已即时广播, 标记已同步
        )
        conn = _conn()
        with conn.cursor() as cur:
            # 30 分钟内重复提交: 覆盖该用户最近一条计划
            cur.execute(
                "SELECT id, filedAt FROM `FlightPlan` WHERE userId=%s "
                "ORDER BY id DESC LIMIT 1", (str(user_id or "")[:4],))
            row = cur.fetchone()
            overwritten = False
            if row and row[1]:
                try:
                    last_time = datetime.datetime.strptime(row[1], '%Y/%m/%d %H:%M:%S')
                except ValueError:
                    last_time = None
                if last_time is not None and (now_dt - last_time).total_seconds() < 1800:
                    cur.execute(
                        "UPDATE `FlightPlan` SET "
                        "callsign=%s, flightType=%s, aircraft=%s, wakeCategory=%s, equipment=%s, "
                        "transponder=%s, depIcao=%s, depTime=%s, cruiseAlt=%s, cruiseSpeed=%s, "
                        "arrIcao=%s, alternate=%s, flightTime=%s, endurance=%s, route=%s, "
                        "remarks=%s, filedAt=%s, syncFsd=%s WHERE id=%s",
                        (data[1], data[2], data[3], data[4], data[5], data[6],
                         data[7], data[8], data[9], data[10], data[11], data[12],
                         data[13], data[14], data[15], data[16], data[18], data[19],
                         row[0]),
                    )
                    overwritten = True
            if not overwritten:
                cur.execute(
                    "INSERT INTO `FlightPlan` "
                    "(userId, callsign, flightType, aircraft, wakeCategory, equipment, "
                    " transponder, depIcao, depTime, cruiseAlt, cruiseSpeed, arrIcao, "
                    " alternate, flightTime, endurance, route, remarks, shareId, filedAt, syncFsd) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    data,
                )
        conn.commit()
        _release(conn)
        return True
    except Exception as e:
        logger.warning(f"FlightPlan insert failed: {e}")
        return False


def amend_flight_plan(callsign, fp):
    """$AM 修正飞行计划: 更新 FlightPlan 中该呼号最新一条计划。"""
    if not callsign or not fp:
        return False
    try:
        import datetime
        now = datetime.datetime.now().strftime('%Y/%m/%d %H:%M:%S')
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM `FlightPlan` WHERE callsign=%s ORDER BY id DESC LIMIT 1",
                (str(callsign)[:191],))
            row = cur.fetchone()
            if not row:
                _release(conn)
                return False
            cur.execute(
                "UPDATE `FlightPlan` SET "
                "flightType=%s, aircraft=%s, depIcao=%s, depTime=%s, cruiseAlt=%s, "
                "cruiseSpeed=%s, arrIcao=%s, alternate=%s, flightTime=%s, "
                "route=%s, remarks=%s, filedAt=%s, syncFsd=%s WHERE id=%s",
                (
                    'IFR' if str(getattr(fp, 'type', 'I'))[:1].upper() == 'I' else 'VFR',
                    str(getattr(fp, 'aircraft', ''))[:191],
                    str(getattr(fp, 'depairport', ''))[:191],
                    str(getattr(fp, 'deptime', '') or '')[:191],
                    str(getattr(fp, 'altitude', ''))[:191],
                    str(getattr(fp, 'tascruise', '') or '')[:191],
                    str(getattr(fp, 'destairport', ''))[:191],
                    str(getattr(fp, 'altairport', ''))[:191],
                    str(getattr(fp, 'flightTime', '') or '')[:191],
                    str(getattr(fp, 'route', ''))[:191],
                    str(getattr(fp, 'remarks', ''))[:191],
                    now,
                    1,
                    row[0],
                ),
            )
        conn.commit()
        _release(conn)
        return True
    except Exception as e:
        logger.warning(f"FlightPlan amend failed: {e}")
        return False


def get_recent_plan(callsign):
    """$CQ FP 请求: 取该呼号最新一份飞行计划（供 EuroScope 拉取）。

    修复(2026-10-16)：
      - 原先先查不存在的 FlightPilot 表取 cid（表缺失时永远返回 None → 永远回空 $FP）；
      - 且用 filedAt 的「30 分钟窗口」+ cid[:4] 截断匹配，提前提交的计划会被丢弃。
    现在直接按呼号取最新一条（与 get_plan_by_callsign 一致），仅对「过旧」的计划设
    24 小时上限，避免把很久以前的计划当成当前计划返回。
    """
    if not callsign:
        return None
    try:
        import datetime
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT callsign, flightType, aircraft, cruiseSpeed, depIcao, "
                "       depTime, cruiseAlt, arrIcao, flightTime, endurance, "
                "       alternate, remarks, route, filedAt "
                "FROM `FlightPlan` WHERE callsign=%s "
                "ORDER BY id DESC LIMIT 1",
                (str(callsign)[:191],))
            row = cur.fetchone()
        _release(conn)
        if not row:
            return None
        cols = ['callsign', 'flightType', 'aircraft', 'cruiseSpeed', 'depIcao',
                'depTime', 'cruiseAlt', 'arrIcao', 'flightTime', 'endurance',
                'alternate', 'remarks', 'route', 'filedAt']
        data = dict(zip(cols, row))
        # 过旧计划不再返回（filedAt 可能是 'YYYY/MM/DD HH:MM:SS' 或 'YYYY-MM-DD HH:MM:SS'）
        filed = None
        raw = str(row[13] or '').strip()
        for fmt in ('%Y/%m/%d %H:%M:%S', '%Y-%m-%d %H:%M:%S', '%Y/%m/%d %H:%M', '%Y-%m-%d %H:%M'):
            try:
                filed = datetime.datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
        if filed and (datetime.datetime.now() - filed).total_seconds() > 86400:
            return None
        return data
    except Exception as e:
        logger.warning(f"Recent plan query failed: {e}")
        return None


def get_online_flight_plans():
    """广播给管制员: 仅在线机组(FlightPilot 快照)的飞行计划(每呼号最新一条)。

    以 FlightPilot 为主表, 只返回当前在线机组且存在飞行计划的记录;
    不在线的机组计划不广播。
    """
    try:
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT fp.callsign, fp.flightType, fp.aircraft, fp.cruiseSpeed, "
                "       fp.depIcao, fp.depTime, fp.cruiseAlt, fp.arrIcao, "
                "       fp.flightTime, fp.endurance, fp.alternate, fp.remarks, fp.route "
                "FROM `FlightPilot` p "
                "JOIN (SELECT callsign, MAX(id) AS max_id FROM `FlightPlan` GROUP BY callsign) t "
                "  ON t.callsign = p.callsign "
                "JOIN `FlightPlan` fp ON fp.id = t.max_id "
                "ORDER BY fp.id DESC LIMIT 200")
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        _release(conn)
        return rows
    except Exception as e:
        logger.warning(f"Online FlightPlan read failed: {e}")
        return []


# ==================== 在线快照: FlightPilot / FlightController ====================

def upsert_pilot(cid, callsign, realname, home, lat, lon, altitude,
                 bank, pitch, heading, altitude_correction, groundspeed,
                 transponder, transpondermd):
    """机组位置更新: upsert 到 FlightPilot 快照表(在线即存在)。

    bank/pitch/heading 为姿态角(度), 来自 FSD pbh 编码: pitch<<22 | bank<<12 | heading<<2。
    altitude_correction = 气压高度 - 真实高度(AltitudeC)。home 为 base 机场。
    """
    if not callsign:
        return False
    try:
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO `FlightPilot` "
                "(cid, callsign, realname, home, latitude, longitude, altitude, "
                " bank, pitch, heading, AltitudeC, groundspd, transponder, transpondermd, updatedAt) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW()) "
                "ON DUPLICATE KEY UPDATE "
                "cid=VALUES(cid), realname=VALUES(realname), home=VALUES(home), "
                "latitude=VALUES(latitude), longitude=VALUES(longitude), altitude=VALUES(altitude), "
                "bank=VALUES(bank), pitch=VALUES(pitch), heading=VALUES(heading), "
                "AltitudeC=VALUES(AltitudeC), "
                "groundspd=VALUES(groundspd), transponder=VALUES(transponder), "
                "transpondermd=VALUES(transpondermd), updatedAt=NOW()",
                (str(cid or '')[:4], str(callsign)[:7], str(realname or '')[:30],
                 str(home or '')[:4], lat, lon, altitude, bank, pitch, heading,
                 altitude_correction, groundspeed, transponder, transpondermd),
            )
        conn.commit()
        _release(conn)
        return True
    except Exception as e:
        logger.warning(f"FlightPilot upsert failed: {e}")
        return False


def upsert_controller(cid, callsign, realname, lat, lon,
                      frequency, facility_type, visual_range):
    """管制位置更新: upsert 到 FlightController 快照表(在线即存在)。"""
    if not callsign:
        return False
    try:
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO `FlightController` "
                "(cid, callsign, realname, latitude, longitude, frequency, "
                " facilityType, visualRange, updatedAt) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,NOW()) "
                "ON DUPLICATE KEY UPDATE "
                "cid=VALUES(cid), realname=VALUES(realname), latitude=VALUES(latitude), "
                "longitude=VALUES(longitude), frequency=VALUES(frequency), "
                "facilityType=VALUES(facilityType), visualRange=VALUES(visualRange), "
                "updatedAt=NOW()",
                (str(cid or '')[:4], str(callsign)[:12], str(realname or '')[:30],
                 lat, lon, frequency, facility_type, visual_range),
            )
        conn.commit()
        _release(conn)
        return True
    except Exception as e:
        logger.warning(f"FlightController upsert failed: {e}")
        return False


def get_username_by_cid(cid):
    """按 CID 取平台用户名（客户端 real name 为空或误填 CID 时兜底）。失败返回空串。"""
    conn = None
    try:
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute("SELECT username FROM `User` WHERE cid=%s", (cid,))
            row = cur.fetchone()
            return (row[0] or "") if row else ""
    except Exception as e:
        logger.warning(f"get_username_by_cid failed: {e}")
        return ""
    finally:
        if conn is not None:
            _release(conn)


def delete_online(callsign):
    """下线: 从 FlightPilot / FlightController 快照表删除该呼号。"""
    if not callsign:
        return
    try:
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM `FlightPilot` WHERE callsign=%s", (callsign,))
            cur.execute("DELETE FROM `FlightController` WHERE callsign=%s", (callsign,))
        conn.commit()
        _release(conn)
    except Exception as e:
        logger.warning(f"Online snapshot delete failed: {e}")


def get_plan_by_callsign(callsign):
    """取指定呼号最新一条飞行计划(官网提交后由管理端口通知广播用)。"""
    if not callsign:
        return []
    try:
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT callsign, flightType, aircraft, cruiseSpeed, depIcao, "
                "       depTime, cruiseAlt, arrIcao, flightTime, endurance, "
                "       alternate, remarks, route "
                "FROM `FlightPlan` WHERE callsign=%s "
                "ORDER BY id DESC LIMIT 1", (callsign,))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        _release(conn)
        return rows
    except Exception as e:
        logger.warning(f"Plan by callsign failed: {e}")
        return []


def get_unsynced_plans():
    """取未广播计划(syncFsd=0), 供周期扫描广播(官网提交后置 0, 广播后置 1)。

    附带 online 标记(该呼号机组是否在 FlightPilot 在线快照中),
    机组不在线时广播只带呼号的空 $FP 占位计划。
    """
    try:
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT fp.id, fp.callsign, fp.flightType, fp.aircraft, fp.cruiseSpeed, "
                "       fp.depIcao, fp.depTime, fp.cruiseAlt, fp.arrIcao, fp.flightTime, "
                "       fp.endurance, fp.alternate, fp.remarks, fp.route, "
                "       (p.callsign IS NOT NULL) AS online "
                "FROM `FlightPlan` fp "
                "LEFT JOIN `FlightPilot` p ON p.callsign = fp.callsign "
                "WHERE fp.syncFsd=0 ORDER BY fp.id ASC LIMIT 50")
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        _release(conn)
        return rows
    except Exception as e:
        logger.warning(f"Unsynced plans read failed: {e}")
        return []


def mark_plan_synced(plan_id):
    """标记计划已广播(syncFsd=1)。"""
    if not plan_id:
        return
    try:
        conn = _conn()
        with conn.cursor() as cur:
            cur.execute("UPDATE `FlightPlan` SET syncFsd=1 WHERE id=%s", (plan_id,))
        conn.commit()
        _release(conn)
    except Exception as e:
        logger.warning(f"Mark plan synced failed: {e}")


def set_plan_locked(callsign, locked: bool):
    """标记/解除 FlightPlan.locked（管制 $AM 修正后锁定；断线或起降机场变更时解锁）。

    移植自 SimpleFSD FlightPlan.Locked 语义，落到 DB 便于后端/前端展示锁状态。
    """
    if not callsign:
        return False
    try:
        conn = _conn()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE `FlightPlan` SET `locked`=%s WHERE `callsign`=%s",
                            (1 if locked else 0, callsign))
            conn.commit()
        finally:
            _release(conn)
        return True
    except Exception as e:
        logger.warning(f"set_plan_locked failed for {callsign}: {e}")
        return False


def add_online_time(cid, is_atc: bool, seconds: int):
    """累加用户在线时长：管制累加 User.totalAtcTime，机组累加 User.totalPilotTime。

    移植自 SimpleFSD UpdateUserAtcTime / UpdateUserPilotTime（它每次掉线结算）。
    我们的结算时机 =「会话真正终结」时（重连窗口内不结算，避免闪断重复计时）。
    """
    if not cid or seconds <= 0:
        return False
    col = "totalAtcTime" if is_atc else "totalPilotTime"
    try:
        conn = _conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE `User` SET `{col}` = `{col}` + %s WHERE `cid`=%s",
                    (int(seconds), str(cid)))
            conn.commit()
        finally:
            _release(conn)
        return True
    except Exception as e:
        logger.warning(f"add_online_time failed for cid={cid}: {e}")
        return False


def record_track(callsign, session_id, lat, lon, altitude, ground_speed, heading, on_ground):
    """记录一个航迹点（whazzup 回放增强）。

    fsd-py 对机组位置包每 10s 采样一次写入 FlightTrack 表，供地图回放/事后核查。
    失败只告警，绝不影响协议处理。
    """
    if not callsign:
        return False
    try:
        conn = _conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO `FlightTrack` "
                    "(`callsign`,`sessionId`,`lat`,`lon`,`altitude`,`groundSpeed`,`heading`,`onGround`) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (callsign, int(session_id or 0), float(lat), float(lon),
                     int(altitude or 0), int(ground_speed or 0), int(heading or 0),
                     1 if on_ground else 0))
            conn.commit()
        finally:
            _release(conn)
        return True
    except Exception as e:
        logger.warning(f"record_track failed for {callsign}: {e}")
        return False


def cleanup_tracks(days=30):
    """清理过期航迹（默认保留 30 天），在服务启动时调用一次。"""
    try:
        conn = _conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM `FlightTrack` WHERE `recordedAt` < NOW() - INTERVAL %s DAY",
                    (int(days),))
                n = cur.rowcount
            conn.commit()
        finally:
            _release(conn)
        return n or 0
    except Exception as e:
        logger.warning(f"cleanup_tracks failed: {e}")
        return 0
