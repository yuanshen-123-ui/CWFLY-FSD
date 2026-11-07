"""FSD Protocol constants - matches fsd-master/fsd/protocol.h"""

import os

# Server-to-server commands
CMD_NOTIFY = 0
CMD_REQMETAR = 1
CMD_PING = 2
CMD_PONG = 3
CMD_SYNC = 4
CMD_LINKDOWN = 5
CMD_NOWX = 6
CMD_ADDCLIENT = 7
CMD_RMCLIENT = 8
CMD_PLAN = 9
CMD_PD = 10   # Pilot data
CMD_AD = 11   # ATC data
CMD_CERT = 12
CMD_MULTIC = 13
CMD_WEATHER = 14
CMD_METAR = 15
CMD_ADDWPROF = 16
CMD_DELWPROF = 17
CMD_KILL = 18
CMD_RESET = 19

CMD_NAMES = [
    "NOTIFY", "REQMETAR", "PING", "PONG", "SYNC", "LINKDOWN",
    "NOWX", "ADDCLIENT", "RMCLIENT", "PLAN", "PD", "AD", "ADDCERT",
    "MC", "WX", "METAR", "AWPROF", "DWPROF", "KILL", "RESET"
]

# Client commands
CL_ADDATC = 0
CL_RMATC = 1
CL_ADDPILOT = 2
CL_RMPILOT = 3
CL_REQHANDOFF = 4
CL_MESSAGE = 5
CL_REQWEATHER = 6
CL_PILOTPOS = 7
CL_ATCPOS = 8
CL_PING = 9
CL_PONG = 10
CL_ACHANDOFF = 11
CL_PLAN = 12
CL_SB = 13
CL_PC = 14
CL_WEATHER = 15
CL_CLOUDDATA = 16
CL_WINDDATA = 17
CL_TEMPDATA = 18
CL_REQCOM = 19
CL_REPCOM = 20
CL_REQACARS = 21
CL_REPACARS = 22
CL_ERROR = 23
CL_CQ = 24
CL_CR = 25
CL_KILL = 26
CL_WDELTA = 27
CL_SECUREAUTH = 28

CL_CMD_NAMES = [
    "#AA", "#DA", "#AP", "#DP", "$HO", "#TM", "#RW", "@", "%",
    "$PI", "$PO", "$HA", "$FP", "#SB", "#PC", "#WX", "#CD", "#WD",
    "#TD", "$C?", "$CI", "$AX", "$AR", "$ER", "$CQ", "$CR", "$!!",
    "#DL", "$SA"
]

# Certificate operations
CERT_ADD = 0
CERT_DELETE = 1
CERT_MODIFY = 2

# Error codes
ERR_OK = 0
ERR_CSINUSE = 1
ERR_CSINVALID = 2
ERR_REGISTERED = 3
ERR_SYNTAX = 4
ERR_SRCINVALID = 5
ERR_CIDINVALID = 6
ERR_NOSUCHCS = 7
ERR_NOFP = 8
ERR_NOWEATHER = 9
ERR_REVISION = 10
ERR_LEVEL = 11
ERR_SERVFULL = 12
ERR_CSSUSPEND = 13

ERR_STR = [
    "No error", "Callsign in use", "Invalid callsign",
    "Already registered", "Syntax error", "Invalid source callsign",
    "Invalid CID/password", "No such callsign", "No flightplan",
    "No such weather profile", "Invalid protocol revision",
    "Requested level too high", "Too many clients connected",
    "CID/PID was suspended"
]

# Client types
CLIENT_PILOT = 1
CLIENT_ATC = 2
CLIENT_ALL = 3

# Feed strategies
FEED_IN = 1
FEED_OUT = 2
FEED_BOTH = 3

# Kill reasons
KILL_NONE = 0
KILL_COMMAND = 1
KILL_FLOOD = 2
KILL_INITTIMEOUT = 3
KILL_DATATIMEOUT = 4
KILL_CLOSED = 5
KILL_WRITEERR = 6
KILL_KILL = 7
KILL_PROTOCOL = 8

# Rating levels
LEV_SUSPENDED = 0
LEV_OBSPILOT = 1
LEV_STUDENT1 = 2
LEV_STUDENT2 = 3
LEV_STUDENT3 = 4
LEV_CONTROLLER1 = 5
LEV_CONTROLLER2 = 6
LEV_CONTROLLER3 = 7
LEV_INSTRUCTOR1 = 8
LEV_INSTRUCTOR2 = 9
LEV_INSTRUCTOR3 = 10
LEV_SUPERVISOR = 11
LEV_ADMINISTRATOR = 12
LEV_MAX = 12

CERT_LEVEL_NAMES = [
    "SUSPENDED", "OBSPILOT", "STUDENT1", "STUDENT2", "STUDENT3",
    "CONTROLLER1", "CONTROLLER2", "CONTROLLER3", "INSTRUCTOR1",
    "INSTRUCTOR2", "INSTRUCTOR3", "SUPERVISOR", "ADMINISTRATOR"
]

# 设施类型(% 报文 facilitytype 字段)→ 开放该席位所需的最低评级。
# 移植自 SimpleFSD RatingFacilityMap: DEL/GND 需 S1, TWR 需 S2, APP 需 S3,
# CTR 需 C1, FSS 需 C3(SimpleFSD 中 CTR3 及以上才含 FSS)。OBS 人人可用。
FACILITY_MIN_RATING = {
    0: LEV_OBSPILOT,      # Observer
    1: LEV_CONTROLLER3,   # FSS
    2: LEV_STUDENT1,      # Delivery
    3: LEV_STUDENT1,      # Ground
    4: LEV_STUDENT2,      # Tower
    5: LEV_STUDENT3,      # Approach/Departure
    6: LEV_CONTROLLER1,   # Enroute/Center
}

# 广播视程上限(海里): 防止客户端上报离谱 visual_range 导致裁剪失效,
# 也避免超大数值参与距离比较(SimpleFSD 无此限制, 这里加严)
MAX_VISUAL_RANGE = 3000

# Protocol constants
CALLSIGN_BYTES = 12
MAXLOG = 50
MAX_LINE_LENGTH = 512
MAX_HOPS = 10
NEED_REVISION = 9
USER_TIMEOUT = 500
SERVER_TIMEOUT = 800
# 读超时：客户端在线时每秒都在上报位置，300s 无任何上行即可判定链路已死。
# 下调(2026-10-16：800→300) 以便更快回收僵尸会话；可用 FSD_CLIENT_TIMEOUT 覆盖。
CLIENT_TIMEOUT = int(os.environ.get("FSD_CLIENT_TIMEOUT", "300"))
SILENT_CLIENT_TIMEOUT = 36000
PING_TIMEOUT = 200
SYNC_TIMEOUT = 120
SERVER_MAX_TOOK = 240

PRODUCT = "FSD Python Server v1.0"
VERSION = "V1.0"
