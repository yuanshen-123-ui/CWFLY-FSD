# 涓存椂 FSD 瀹炰緥锛?60.202.241.240锛?
> 2026-11-07 鏃ф湇鍔″櫒 SSH 琚攣鏈熼棿鐨勪复鏃惰縼绉汇€?*鏃ф湇鍔″櫒锛?8.76.182.6锛変笂鐨?FSD 浠嶅湪杩愯**锛孌NS 鏈垏鎹㈠墠涓嶅彈褰卞搷銆?
## 鍩虹淇℃伅

| 椤?| 鍊?|
|---|---|
| 涓绘満 | `160.202.241.240`锛圲buntu 24.04锛宺oot 鍙?key 鐧诲綍锛?|
| 鐩綍 | `/opt/cwfly-fsd-cwfly`锛堟柊鍔犲浐鐗堜唬鐮侊紝`fsd/` 鍖呯粨鏋勶級 |
| systemd | `cwfly-fsd-cwfly.service`锛坋nabled锛孯estart=always锛?|
| 瀹㈡埛绔鍙?| `6809`锛堝叕缃戯紝ufw 宸叉斁琛岋級 |
| 鏈嶅姟鍣ㄤ簰鑱?| `3011`锛堜粎 127.0.0.1锛?|
| 绠＄悊鎺у埗鍙?| `3012`锛堜粎 127.0.0.1锛?|
| 鏃ュ織 | `journalctl -u cwfly-fsd-cwfly` |

## 鏁版嵁鏉ユ簮锛堥噸瑕侊級

- 璇ュ疄渚?*涓嶈繛 MySQL**锛坄fsd.conf` 鐨?`[database]` 鐣欑┖ 鈫?DB 鍔熻兘闈欓粯绂佺敤锛夈€?- 璇佷功鏉ヨ嚜鏈満 PostgreSQL `cwfly` 搴擄紙`ts-postgres` 瀹瑰櫒锛変腑鐨?**User 蹇収**锛?2 涓处鍙?+ 闅忔満鍙ｄ护鐨?`000000` 绠＄悊鍛橈紝鍏?23 鏉★紝鍐欏叆 `cert.txt`锛?00锛夈€?- 鍥犳锛?*蹇収涔嬪悗鏂版敞鍐岀殑鐢ㄦ埛銆佹垨宸蹭慨鏀硅繃瀵嗙爜鐨勭敤鎴凤紝鍙兘鏃犳硶鐧诲綍**锛涚瓑鏃ф湇鍔″櫒鎭㈠鍚庨渶瑕侀噸鏂板悓姝?`cert.txt`锛堟垨鏀圭敤 DB 妯″紡锛夈€?- 鏃?DB 鏃讹細鍦ㄧ嚎鍒楄〃/浣嶇疆/whazzup/璁よ瘉姝ｅ父锛涢琛岃鍒掕惤搴撱€佸湪绾挎棩蹇椼€佽瘉涔︾儹鍔犺浇鏆傚仠銆?
## 瀵嗙爜/鍑嵁鏂囦欢锛坮oot-only锛?
- 绠＄悊鍙帮紙3012锛夊彛浠わ細`/root/cwfly-fsd-secrets/fsd-tmp-secrets.txt`
- 绠＄悊鍛?`000000` 鐨?FSD 鍙ｄ护锛歚/opt/cwfly-fsd-cwfly/.fsd-admin-000000`
- 閰嶇疆锛歚/opt/cwfly-fsd-cwfly/fsd.conf`锛?00锛?
## 鍒囨崲 DNS锛堝緟鍔烇級

瀹㈡埛绔敤鐨勬槸 `fsd.cwflysim.cn`锛堝綋鍓?A = `38.76.182.6`锛夈€備复鏃跺垏鍒版湰鏈洪渶瑕佹妸璇?A 璁板綍鏀逛负 `160.202.241.240`锛圖NSPod锛歚ns3.dnsv2.com` / `ns4.dnsv2.com`锛夛紝TTL 寤鸿 60鈥?20 绉掋€?
鍒囨崲鍓嶅鎴风浠嶈繛鏃ф湇鍔″櫒锛涘垏鎹㈠悗锛?- 鏃ф湇鍔′笂鐨勫湪绾跨敤鎴蜂細鎺夌嚎锛?- 涓荤珯銆屽湪绾挎暟鎹?璁″垝涓嬪彂銆嶄粛鎸囧悜鏃?FSD锛?*涓よ竟涓嶅悓姝?*锛堜复鏃剁幇璞★級銆?
## 鍥炴粴

```bash
# 鍋滀复鏃跺疄渚?systemctl disable --now cwfly-fsd-cwfly.service

# 鎭㈠ target 涓婂師鏈夌殑 CWTS 澶囦唤 FSD锛堝彲閫夛級
cp -a /opt/cwfly-fsd /opt/cwfly-fsd.bak-<TS>   # 宸叉湁澶囦唤
systemctl enable --now cwfly-fsd.service

# 鏃ф湇鍔″櫒 38.76.182.6 鐨?FSD 涓€鐩存湭鍔紝DNS 鏀瑰洖鍗冲彲
```

## 宸茬煡闄愬埗

1. `[weather] source = none`锛坱arget 鐨?DNS 瀵?NOAA 鍩熷悕瑙ｆ瀽寮傚父锛夛紝涓存椂瀹炰緥**娌℃湁 METAR/澶╂皵**銆?2. 鏃?MySQL锛氳埅鐝鍒?鍦ㄧ嚎鏃ュ織涓嶈惤搴撱€?3. 璇佷功蹇収涓?2026-08 宸﹀彸鏁版嵁锛岄渶鍚庣画鍚屾銆?4. 鍘?target 涓婄殑 `cwfly-fsd.service`锛圕WTS 澶囦唤锛屾渶鍚庡鎴风娲诲姩 2026-09-28锛夊凡 stop/disable锛屽浠藉湪 `/opt/cwfly-fsd.bak-*`銆?
## 2026-11-07 应急切换（HK 机房临时维护）

因香港机器需等到次日，全部客户端临时切到本机；证书只有 8 月快照，无法覆盖所有当前用户，因此启用**应急认证模式**（systemd drop-in `/etc/systemd/system/cwfly-fsd-cwfly.service.d/emergency.conf`）：

```ini
[Service]
Environment=FSD_ALLOW_ANY_PASSWORD=1
Environment=FSD_AUTO_ADD_UNKNOWN=1
```

- 已知 CID：**任意密码可登录**（保留原等级）；
- 未知 CID：自动按 `OBSPILOT(level=1)` 注册，避免 `CID/PID was suspended`；
- 新代码开关默认关闭，只有该 drop-in 开启；**恢复 HK / 同步真实哈希后必须删除 drop-in 并重启**；
- 客户端统一地址：`fsd.cwflysim.cn:6809` 或 `160.202.241.240:6809`。

### 恢复步骤（HK 回来后）

1. 在 HK 旧机导出 `User.cid/fsdPasswordHash/fsdLevel`，重新生成 `cert.txt`（覆盖快照）；
2. `rm /etc/systemd/system/cwfly-fsd-cwfly.service.d/emergency.conf && systemctl daemon-reload && systemctl restart cwfly-fsd-cwfly`；
3. 客户端统一切回（或保持本机，按最终方案）；
4. 用真实账号验证登录 + 管制可见机组。