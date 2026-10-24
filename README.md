# DockerCopilot 助手全家桶

管理 DockerCopilot 的两种方式，按需选一种即可：

| | 方式一：MP 插件 | 方式二：独立 Docker 面板（本项目） |
|---|---|---|
| 适合人群 | 已经在用 [MoviePilot](https://github.com/jxxghp/MoviePilot) 的人 | 不用 MP、只想管理 DC 的人 |
| 安装位置 | 装在 MoviePilot 插件市场里 | 独立 Docker 容器 |
| 入口 | MP 插件页 | 独立网页 `http://IP:端口` |
| 依赖 | 需要 MoviePilot | 无任何依赖 |

**核心功能相同**：多台 DC 设备管理、容器列表、单个/批量一键更新、自更新、近 30 天更新统计。

**自动更新与通知**：面板的「自动更新」页会按 cron 周期查询各台 DC 的 `haveUpdate`，对勾选的容器写入更新记录并发送通知；配置自动更新后，会按 DockerCopilot 的真实接口提交 `containerName` 与 `imageNameAndTag`，支持自动更新开始通知、最终成功/失败通知、进度汇报、轮询间隔和次数、清理镜像开关。通知去重按设备/容器/镜像指纹保存，更新完成后会清除待处理状态。直接在 DockerCopilot 网页中更新不会自动产生面板记录，因为 DockerCopilot 没有向面板回调；需要通过面板或 MP 插件执行，或点击面板「立即检查一次」补查。

**数据互通**：两种方式共用同一份 `dc-helper` 格式 JSON——MP 插件「导出配置」下载的文件可以直接在 Docker 面板「数据迁移 → 导入设备」使用，反之面板「导出设备」的文件也能喂给 MP 插件导入，两边自由搬家、地址重复自动跳过。

**面板独有**：登录账号（`PANEL_USERNAME`）+ 密码、自定义背景图上传（默认星链粒子动画背景，卡片毛玻璃）、手机端 Apple 风格毛玻璃适配、**独立通知推送**（企业微信群机器人 + Telegram，容器更新成功/失败自动推送，Telegram 可配代理，页面上有一键测试）。

面板按 DockerCopilot 提供的标准 API 管理设备和业务容器。更新 DockerCopilot 自身时，只有后端明确提供安全的自更新接管机制才会显示相应入口；普通后端不会被强行停止。

---

## 方式二：Docker 部署（本项目）

### 方式 A：docker compose（推荐）

```yaml
services:
  dc-panel:
    image: <dockerhub-user>/dc-panel:latest   # Docker Hub 镜像
    container_name: dc-panel
    restart: unless-stopped
    environment:
      - PANEL_PASSWORD=<你的密码>       # 登录密码，必设
      - PANEL_USERNAME=<你的账号>       # 登录账号，默认 admin
      - PANEL_PORT=<容器内端口>         # 容器内监听端口，需与下面 ports 右侧一致
    ports:
      - "<宿主机端口>:<容器内端口>"      # 例：8080:8080
    volumes:
      - ./data:/data                    # 配置与更新记录
```

把三个 `<...>` 换成自己的值即可，例如密码 `my-secret-pwd`、端口 `8080`：

```yaml
    environment:
      - PANEL_PASSWORD=my-secret-pwd
      - PANEL_PORT=8080
    ports:
      - "8080:8080"
```

浏览器访问 `http://宿主机IP:<宿主机端口>`，密码即 `PANEL_PASSWORD`。

### 方式 B：docker run 一条命令

```bash
docker run -d --name dc-panel --restart unless-stopped \
  -e PANEL_PASSWORD=<你的密码> \
  -p <宿主机端口>:<容器内端口> \
  -v <宿主机目录>:/data \
  <dockerhub-user>/dc-panel:latest
```

> 如果同时发布到其他 OCI Registry，请把示例中的镜像地址替换为你的实际地址。

### 方式 C：群晖 / 飞牛 / UNRAID 等图形界面

1. 镜像拉取：注册表搜 `dc-panel`（或手动下载 `<dockerhub-user>/dc-panel:latest`）
2. 端口映射：本地 `<宿主机端口>` → 容器 `<容器内端口>`（两者填一样的值最省事）
3. 环境变量：`PANEL_PASSWORD` = 你的密码（`PANEL_PORT` 与容器内端口保持一致）
4. 挂载目录：随便一个宿主机目录 → 容器 `/data`（存配置和更新记录）
5. 启动即可，其余无特殊要求

### 镜像拉取加速（国内可选）

GHCR 直连慢时，可套用任意镜像加速前缀拉取，或在 daemon 里配置 `registry-mirrors` 后照常 pull：

```bash
docker pull <dockerhub-user>/dc-panel:latest
# 拉得慢时再套加速前缀：<你的加速站地址>/<dockerhub-user>/dc-panel:latest
```

### 更新

镜像由 GitHub Actions 自动构建（amd64 + arm64），更新只需：

```bash
docker compose pull && docker compose up -d    # compose 部署
# 或
docker pull <dockerhub-user>/dc-panel:latest && docker restart dc-panel   # 部分环境需重建容器
```

> **密码/账号规则（重要）**：
> - **环境变量只是初始值**：`PANEL_PASSWORD` / `PANEL_USERNAME` 只在**从未用 UI 修改过**时生效（每次启动按变量重置，改变量即可改密码、忘密码也能救回）。
> - **UI 修改一次，变量即永久失效**：在页面里改过密码或账号后，以 UI 为准（PBKDF2-SHA256 加盐哈希持久存 `/data/panel.json`），此后 `PANEL_PASSWORD` / `PANEL_USERNAME` 都不再起作用，重启也不变。
> - **什么变量都不设**：账号默认 `admin`；密码首次启动**随机生成并打印在容器日志里**（`docker logs dc-panel` 查看），登录后在页面里改成自己的即可。
> - 2FA 密钥同样持久保存在 `/data/panel.json`，与 `/data` 卷一起持久化，重启不丢。
> - 想恢复"变量接管"：删掉 `/data/panel.json` 里的 `ui_pwd` / `ui_user` 字段（或整个文件重置）后重建容器。

添加 DC 设备时填：**地址**（如 `dc-host.example:12712`）+ **secretKey**（即该 DC WebUI 的登录密码）。

## 方式一：MP 插件

MoviePilot 插件的仓库地址和安装方法请以实际发布仓库为准。
