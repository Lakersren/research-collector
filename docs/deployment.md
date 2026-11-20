# A 采集服务部署

将 `research-collector.tar.gz` 上传到 A 的 `~/Downloads`，用日常运维账号执行。需要重新打包时，在本地仓库根目录运行 `python3 deploy/build_release.py research-collector`，包在 `dist/`。

## 首次安装

```bash
sudo mkdir -p /opt/app
sudo tar -zxf ~/Downloads/research-collector.tar.gz -C /opt/app
cd /opt/app/research-collector
bash setup.sh
bash start.sh
```

`setup.sh` 初始化 Python、注册服务并生成 `etc/collector.env`。默认直连外部网站，管理接口监听本机 `127.0.0.1:8000`。如果服务器通过代理出网，在启动前修改 `etc/collector.env`。`start.sh` 会安装依赖并启动服务。

## 已安装，更新代码

如果 A 使用 Git 部署，在 systemd 采集服务的部署目录中执行。

```bash
git pull --ff-only && bash start.sh
```

`start.sh` 会核对服务目录、安装依赖并重启采集服务。

等当前采集结束，上传新包后执行以下命令。直接覆盖同名文件，保留旧项目目录里的配置和数据。

```bash
sudo systemctl stop research-collector.service &&
tar --overwrite -zxf ~/Downloads/research-collector.tar.gz -C /opt/app &&
bash /opt/app/research-collector/start.sh
```

## 验收

```bash
curl -fsS http://127.0.0.1:8000/health
```

看到 `"status":"ok"` 表示服务已启动。接着运行试采脚本。

```bash
bash /opt/app/research-collector/test-source.sh
bash /opt/app/research-collector/test-source.sh 7
bash /opt/app/research-collector/test-source.sh all
```

第一条列出来源供选择，第二条直接采集编号 7，第三条采集全部来源。每个来源只检查清单中的前 10 篇，保存北京时间昨天发布的文章。整轮结束后打印报告和日期归档路径。

系统也可以用管理令牌调用 `POST /jobs/today`。传 `{"scope":"all"}` 采集全部来源，传 `{"scope":"source","source_id":"web:servethehome"}` 采集一个来源。接口返回 HTTP 202 和任务 ID，用 `GET /jobs/{job_id}` 查询进度。已有采集任务运行时返回 HTTP 409。来源 ID 可通过 `GET /sources` 获取。

打开脚本打印的 `data/archive/YYYYMMDD/report.md` 查看采集任务完成表。表头显示目标日期、执行时间和日期核对结果，下面是来源汇总和逐篇明细。同名 JSON 保留供程序读取。发布日期无法确认时记为失败，日期不符时跳过。启动失败看 `sudo journalctl -u research-collector.service -n 50 --no-pager`。

默认每天北京时间 01:00 采集昨天发布的文章，保留最近 7 个目标发表日的数据。
