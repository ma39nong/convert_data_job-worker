# ROS2 bag 清洗与 LeRobot v2.1 转换

面向双 FR3 搭配 Wuji 或 Sharpa 灵巧手的离线数据处理工具。根据双臂实测关节状态自动删除录制中的静止区间，再将清洗后的数据转换为包含 RGB、深度及可选触觉的 LeRobot v2.1 数据集。

```text
原始 ROS2 bag → 输入完整性检查 → 静止段裁剪 → 30 Hz 对齐 → LeRobot v2.1 → 输出校验
```

处理阶段原始数据以只读方式访问；采集端daily模式在服务器备份核验通过后释放本机原始副本。裁剪、转换和校验均可独立运行，也可通过可恢复的本地 job 串联执行。

## 工作站 GUI：首次配置与日常使用

工作站界面入口是“工作站清洗与上传控制台”，代码位于 `src/convest/gui/`，开发分支为 `auto-convert-transfer-ui`。GUI 统一管理本机读取、数据服务器备份、24 小时完整 LeRobot 批次和训练服务器 normal/speedup 上传。它使用独立于旧命令行服务的任务配置和运行目录；完整操作说明见 [GUI 使用说明](docs/gui.md)。

### 启动界面

在包含 GUI 代码的工程 checkout 中执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[conversion,training]'
./scripts/start_gui.sh
# 只启动本机网页服务，不自动打开浏览器：
# ./scripts/start_gui.sh --no-browser
```

打开 `http://127.0.0.1:8765/`。启动网页本身不会开始清洗或上传；保存配置后，点击“开始当前任务”或“开始全部任务”才启动对应工作流。界面仅供本机访问。

本机需要 SSH、rsync 以及转换所需环境；服务器需要 Python 3 和 rsync。SSH 使用本机已有配置和密钥，首次连接须核实主机指纹。若已有旧采集/后处理服务正在监听同一批输入，应先正常停止并等当前作业收尾，再由 GUI 接管，避免两套任务重复处理。安装桌面启动器及接管步骤见 [GUI 使用说明](docs/gui.md)。

### 首次部署：配置工作机到服务器的 SSH 公钥认证

GUI 通过 SSH 浏览目录、校验及上传数据，使用 `BatchMode=yes` 和 `StrictHostKeyChecking=yes`，不会弹窗输入服务器密码。因此，**每台实际运行 GUI 的工作机、运行 GUI 的本机用户，都必须先配置好到目标服务器登录账号的公钥认证，并核实服务器主机指纹**。开发电脑配置成功不代表其他 Sharpa/Wuji 工程机也已配置；服务器上的公钥授权属于具体登录账号，不是整台服务器通用。

私钥保留在工作机，只有对应的 `.pub` 公钥加入服务器账号的 `~/.ssh/authorized_keys`。不要把私钥复制到服务器、工程仓库或发给其他人。GUI 不保存密码、私钥内容或私钥口令。

1. **在实际工作机上准备密钥。** 已有可用密钥可以直接复用，不要覆盖。若没有，可在运行 GUI 的用户终端生成一对专用密钥；以下文件名只是示例，若已存在就跳过生成：

   ```bash
   mkdir -p ~/.ssh
   chmod 700 ~/.ssh
   ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519_data_workflow -C "data-workflow-workstation"
   ```

   私钥为 `~/.ssh/id_ed25519_data_workflow`，公钥为同名 `.pub` 文件。生成时可设置私钥口令；带口令密钥需要在后面的 SSH agent 步骤中解锁。

2. **把公钥授权给两台服务器的实际登录账号。** 以下以数据服务器 `user`、训练服务器 `mengxiang`、训练端口 `28` 为例：

   ```bash
   ssh-copy-id -i ~/.ssh/id_ed25519_data_workflow.pub -p 22 user@192.168.10.202
   ssh-copy-id -i ~/.ssh/id_ed25519_data_workflow.pub -p 28 mengxiang@47.116.73.163
   ```

   首次连接时，先向管理员核实显示的主机指纹，再确认并记录到本机 `known_hosts`。按提示输入对应服务器账号的密码完成授权；如果服务器不允许密码登录，请把 `.pub` 公钥交给管理员，由管理员加入对应账号的 `authorized_keys`，并确认服务器端 `~/.ssh`、`authorized_keys` 的属主和权限正确（通常分别为 `700`、`600`）。

   `mengxiang` 是示例账号；实际使用 `tairan`、`junyu`、`zhiwei` 等账号时，替换为真实 SSH 登录用户，并在 GUI 中填同一个用户。若训练连接改为端口 `26`，也要针对该端口连接到的账号完成授权和验证，不要假定 `28` 的授权一定适用。

3. **让 SSH 能找到密钥，必要时解锁私钥。** 上面的专用文件名不是 SSH 默认密钥文件名，需要在现有 `~/.ssh/config` 对应 Host 配置中指定。例如：

   ```sshconfig
   Host 192.168.10.202 47.116.73.163
       IdentityFile ~/.ssh/id_ed25519_data_workflow
       IdentitiesOnly yes
   ```

   若已有相关 Host 配置，应合并到现有配置，不覆盖整个文件；复用其他密钥时填写对应私钥路径。`~/.ssh/config` 和私钥权限通常设为 `600`。

   带口令的私钥先加入当前登录会话的 SSH agent：

   ```bash
   # 当前会话没有可用 agent 时，先执行：eval "$(ssh-agent -s)"
   ssh-add ~/.ssh/id_ed25519_data_workflow
   ```

   然后从同一个终端启动 `./scripts/start_gui.sh`。如果使用桌面启动器，GUI 也必须能继承可用的 `SSH_AUTH_SOCK`；另一个终端中解锁密钥，不代表已打开的 GUI 自动获得该 agent。不要把私钥口令写入配置文件。

4. **用与 GUI 一致的非交互方式验证，再配置界面。** 以下两条命令应直接成功，无需输入密码、私钥口令或再次确认主机指纹，并输出服务器上的 Python 3、rsync 路径：

   ```bash
   ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -p 22 user@192.168.10.202 'command -v python3 && command -v rsync'
   ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -p 28 mengxiang@47.116.73.163 'command -v python3 && command -v rsync'
   ```

   之后在 GUI 的两个“SSH 连接设置”中填写相同的地址、用户和端口，点击“测试连接”，再浏览选择实际上传父目录。登录账号还需对目标父目录有读取、创建和写入权限；SSH 认证成功不等于目标目录可写。

常见问题：`Permission denied (publickey)` 通常需要核对公钥是否加入正确账号、私钥是否被 SSH 找到，以及 agent 是否已解锁；`Host key verification failed` 需要先通过普通 SSH 核实主机指纹及 `known_hosts`，不能关闭指纹校验来绕过；更换工作机或运行 GUI 的本机用户后，需要重新确认以上步骤。

### 工作机原始录制保存路径

先在录制软件中设置本机 ROSbag 的实际保存位置，再让 GUI 读取相同位置。GUI 的“本地读取父目录”只决定监听哪里，不会替录制软件修改保存位置。

本机当前使用以下布局；其他工作机可以更换磁盘、用户名和根目录，但需要保留 `场景/A或B/episode数字/` 层级：

| 手型 | 本机原始录制根目录 |
| --- | --- |
| Sharpa | `/home/user/franka_teleop_data/bags/tomato-sharpa` |
| Wuji | `/home/user/franka_teleop_data/bags/tomato-wuji` |

例如 Sharpa 的 `no-block-one` 场景：

```text
录制软件写入 A：/home/user/franka_teleop_data/bags/tomato-sharpa/no-block-one/A/episode100/
录制软件写入 B：/home/user/franka_teleop_data/bags/tomato-sharpa/no-block-one/B/episode200/
GUI 本地读取父目录：/home/user/franka_teleop_data/bags/tomato-sharpa/no-block-one
```

其他场景把 `no-block-one` 换成 `full-block-more`、`full-block-one` 或 `no-block-more`；Wuji 把 `tomato-sharpa` 换成 `tomato-wuji`。选择父目录同时读取 A/B，选择其中的 A 或 B 则只读取该组。

同事电脑上的用户名或录制路径可能不同，应在其录制软件与 GUI 中填写同事电脑的实际路径，不直接照抄 `/home/user/`。同时检查该磁盘的可用空间，以及运行录制程序和 GUI 的本机账号是否有对应读写权限。

原始录制与本地转换输出分开保存。以工程位于 `/home/user/convert_data_job-worker` 为例，GUI 转换产物位于 `/home/user/convert_data_job-worker/outputs/gui/runs/<任务ID>/<运行版本>/`：`capture/batches/` 保存完整 LeRobot，`training/datasets/` 保存 normal/speedup，`capture/work/` 为处理中间目录。转换输出由程序管理，不需要填进“本地读取父目录”；它占用工程所在磁盘的空间。

### 01 本机读取：四个字段怎样选择

按“项目＋手型＋场景”配置一个持续任务，不需要每产生一条录制就重新填写。

| 字段 | 用途与填写方式 |
| --- | --- |
| 项目 | 默认 `tomato`，参与默认上传目录生成；采摘番茄时可以沿用。当前不会自动切换训练任务描述或转换配置。 |
| 手型 | 选择 Sharpa 或 Wuji，决定转换配置及原生维度，程序不从 bag 自动猜测手型。 |
| 任务名称 | 界面上的区分标签，可沿用默认值或改成易识别的名称；不决定服务器路径、批次名称，也不等同于转换配置中的任务语言描述。 |
| 场景名称 | 可选择 `full-block-more`、`full-block-one`、`no-block-more`、`no-block-one`，也支持自定义单层目录名。通过“浏览”选择本地标准场景目录或其 A/B 时，会自动填入对应场景；未知目录名需自行确认。 |

本地输入示例：

```text
<采集根目录>/
├── full-block-more/
├── full-block-one/
├── no-block-more/
└── no-block-one/
    ├── A/
    │   └── episode100/      # 完整 ROS2 bag 目录
    └── B/
        └── episode200/
```

“本地读取父目录”选择 **`<采集根目录>/no-block-one`**，自动接入它下面的 A、B；也可以只选择 `no-block-one/A` 或 `no-block-one/B`。不要选择某个 `episode100`，也不要选包含四类场景的总父目录：当前只接入直接 A/B，不递归跨场景发现录制。同时监听多个场景时分别“添加”任务，启用任务的输入不能相互包含或重叠。

保存并启动后，GUI 默认每 5 秒观察所选 A/B 下的 `episode数字/` 目录，至少 3 次一致且稳定 60 秒，再检查录制完成状态、metadata、SQLite 分卷、必需话题/消息和输入哈希。只有完整稳定的包才处理，仍在写入的包继续等待。

“包含目录中已有的录制”控制新队列首次接入时是否纳入已有包：勾选接收已有及后续新增包，取消则登记并跳过首次接入时已有的包。该历史选择持久化，重启不会重新计算；修改输入或其他身份配置而产生新运行版本时，需重新核对接入范围，旧运行目录继续保留。

### 跨项目接入：项目名称不等于训练任务描述

当前转换配置按手型加载：[configs/sharpa.yaml](configs/sharpa.yaml) 和 [configs/wuji.yaml](configs/wuji.yaml)。两份文件的 `task` 字段目前都是番茄任务描述，含义为“右手拿起番茄串，随后左手摘取樱桃番茄并放入左侧篮子”。这段语言描述会写入 LeRobot 的任务元数据；GUI 的“任务名称”只是显示标签，不会替换它。

**仅把 GUI 项目从 `tomato` 改为 `grape`，会更新自动上传目录，但训练任务描述仍然是番茄。** 当前 GUI 尚未实现“项目 → 独立转换配置/训练任务描述”的关联。路径可以手动选择，不能据此认为整个转换流程已自动完成葡萄等新项目的适配。

描述错误不会直接改变图像、关节维度、动作数值或时间对齐，但属于训练语义标注错误：实际采葡萄却标注采番茄，会使语言指令与示教动作不匹配。文件结构、视频和 SHA256 校验通过也不能证明语言描述正确。番茄项目同样需要核对描述中的左右手分工、对象和放置位置是否符合实际示教。

接入葡萄等新项目之前，应先确定对应的训练任务描述，核对采集话题、相机、手型维度及数据格式，并补齐项目与独立配置的关联，再用少量真实数据验证完整 LeRobot 和 normal/speedup 的任务元数据及转换质量。当前两种手型的配置由对应任务共用，不应只为葡萄改写共享 `task` 后继续同时处理番茄。修改运行源码或转换配置前，先正常停止相关任务并等待当前作业收尾；已有数据不会因改配置而自动修正标签。

### 202 与训练端：自动生成或手动选择目录

默认勾选“生成默认上传路径”，项目、手型、场景变化会同步生成四个目标路径。以 `tomato / sharpa / no-block-one` 为例：

| 内容 | 自动生成的服务器父目录 |
| --- | --- |
| 202 原始 rosbag | `/data/data-demo/real/tomato/data/rosbag/sharpa/no-block-one` |
| 202 完整 LeRobot | `/data/data-demo/real/tomato/data/lerobot/sharpa/no-block-one` |
| 训练 normal | `/mnt/data/train_platform/datasets/tomato_sharpa/normal/no-block-one` |
| 训练 speedup | `/mnt/data/train_platform/datasets/tomato_sharpa/speedup/no-block-one` |

自动模式下路径输入框只读，**“浏览”仍可查看服务器目录**。确认选择某个目标目录后，程序自动取消“生成默认上传路径”，切换到手动模式，其余三个目标保持原值。也可以先取消该选项再输入路径。手动模式中，修改项目/场景不会覆盖已选路径；重新勾选自动模式则会用当前字段重新生成全部四个路径。更改后都需要保存才生效。

远端同样选择到 **场景目录这一层**，程序只追加 A/B 和包名，不再重复追加项目、手型或场景。目录框填写服务器原生绝对路径 `/data/...` 或 `/mnt/...`，不填写文件管理器的整个 `sftp://user@192.168.10.202/...` 地址。GUI 直接通过 SSH 浏览和上传，不要求先配置旧命令行方案的 SSHFS 挂载。

“SSH 连接设置”中的地址、登录用户、端口用于目录浏览、连接测试及上传，与存储目录分别配置。202 默认 `user@192.168.10.202:22`；训练端默认 `mengxiang@47.116.73.163:28`。训练卡片标题旁可选 26/28，切换仅改变端口，地址、用户、路径不变，并同步下方 SSH 设置；保存后工作流才使用新端口。“已连接”表示 SSH/rsync 检查通过，不代表已经开始采集处理。

### 24 小时批次与统一命名

A/B 各自从首条成功入批开始滚动累计 24 小时，不是每天固定零点上传；重启不重置计时。封存前允许符合边界规则的在处理包完整完成，封存与整批校验通过后才上传完整 LeRobot，并交给本地 normal/speedup 后处理。原始 rosbag 的备份不必等满 24 小时。

GUI 新建批次使用以下对外名称：

```text
202 原始包：<raw_root>/A/episode100/
202 完整包：<lerobot_root>/A/1027_100_200_data_lerobot/
训练 normal：<normal_root>/A/1027_100_200_normal_lerobot/
训练 speedup：<speedup_root>/A/1027_100_200_speedup_lerobot/
```

- `1027` 为本批最后一个包成功完成清洗、转换并入批的日期，采用北京时间 `MMDD`；不是上传日期或封存日期。跨午夜按最后一次成功入批的完成日期取值，恢复或重传不会改变该日期。
- `100_200` 为本批源 episode 最小、最大编号，允许缺号，不代表一定有 101 条录制；真实来源以批次清单和台账为准。每批 LeRobot 内部 episode 索引仍从 0 开始。
- 完整 LeRobot、normal、speedup 使用同一日期和编号范围。原始 rosbag 保持原 episode 目录名。
- 已有批次及已有上传任务保留旧名称；新规则不重建采集队列、不搬移历史结果。内部不可变版本和配对校验证据仍保留身份标识，不同内容的同名目标会报冲突，不能覆盖。

normal 保留全部轨迹/帧、原生关节维度及 RGB 视频字节，移除训练不使用的深度、触觉等字段；speedup 做轨迹时间重采样，保留原始帧率及分辨率。两版各自暂存、全文件 SHA256 核验后发布，两版都完成才标记训练批次已发布。两侧 `.workflow-pairs/` 保存整对完成证据。上传完成后不自动启动训练。

### 训练上传：OSS 自动中转与清理

GUI 的训练上传默认使用以下链路，normal、speedup 和双版本配对材料均按此交付：

```text
工作机本地成品
  → 28：/mnt/public_data/local-realrobot-train-data/<相对路径>
  → 28：/mnt/data/train_platform/datasets/<相对路径>
  → 最终完整校验、持久化完成凭据 → 自动删除本次中转批次
```

例如同一个 normal 批次先写入
`/mnt/public_data/local-realrobot-train-data/tomato_sharpa/normal/no-block-one/A/1027_100_200_normal_lerobot/`，
再由服务器直接搬运到
`/mnt/data/train_platform/datasets/tomato_sharpa/normal/no-block-one/A/1027_100_200_normal_lerobot/`。
两处相对目录完全一致，speedup、Wuji、B 同理。界面仍填写和预览**最终训练路径**，不要改填中转路径。

- 复用训练服务器现有的 `fuse.ossfs2` 挂载 `/mnt/public_data`；最终目录位于 CPFS 的 NFS 挂载 `/mnt/data`。任一挂载未就绪会报错并保留数据，不会悄悄写到普通本地目录。实际登录账号须可写两侧目标子目录。切换到 26 时，也需该端口对应环境具备相同挂载和权限。
- 工控机通过已有 SSH 密钥将文件顺序写入 OSS 挂载；不用给工控机安装 `ossutil` 或配置云密钥，也无需手动执行 DataFlow Import。OSS 不支持 rsync 所需的 `ftruncate`，因此这一步采用流式上传；202 的现有 rsync 逻辑不变。
- 中转上传关闭文件并回读全 SHA256 后，服务器复制到 CPFS 暂存目录，校验精确文件清单、大小及全 SHA256，刷盘、原子发布并持久化回读最终凭据，之后才删除该批中转文件。两个最终版本和配对材料全部交付后，GUI 才计为已发布。
- **清理自动执行，无需手工操作，也不受“释放本机”两个开关控制。** 只处理有本次持久化归属和匹配清单的中转批次；不清空公共根，不删除旁边项目。空的项目/场景/A/B 父目录可能保留。最终目录和远端审计凭据保留。
- 断线、搬运/校验/凭据写入或清理失败会保留数据并进入上传重试。重试跳过已完整校验的中转文件；未完成文件从头重传。最终目录已校验发布时复用最终副本、继续未完成的清理；清理也支持中途重启。发现未知文件或不同内容的同名目录会报冲突并保留数据。
- 保留原有 24 小时封存规则；成品就绪后立即中转，不再额外等待一个周期。正常停止仍等当前上传/搬运/清理操作收尾；任务停止期间不会后台强行清理，重新开始后恢复。

`outputs/gui/settings.json` 新增独立交付设置（不参与采集/训练队列指纹）：

```json
"training_delivery": {
  "mode": "oss",
  "transit_root": "/mnt/public_data/local-realrobot-train-data",
  "final_root": "/mnt/data/train_platform/datasets"
}
```

旧 GUI 设置缺少此项时自动采用上述 OSS 默认值，下次保存时写入；启停任务会沿用原运行版本、转换结果和队列，不重新处理已上传历史批次。已完成的最终副本不会为了经过 OSS 再传一遍。旧运行中的 spec 不会热改，更新前须正常停止，再重新开始。手选训练目标必须位于 `final_root` 的 `项目/normal或speedup/场景` 层级。需要兼容原先其它位置的目标时，可在任务停止后将交付 `mode` 明确设为 `direct`，保存再启动；禁止自动回退直传来掩盖 OSS 故障。旧命令行配置与历史独立作业仍保持原上传方案。

训练数据卡片的“OSS 中转”默认折叠，展开可看当前任务对应路径；实际上传进度显示上传中转、搬运、校验、清理、完成，失败时标明所处阶段及重试原因。“本次中转位置”显示实际批次路径；只有清理确认成功才显示“中转已清理”。

远端最终目录父级的 `.workflow-transfer/` 保存 SHA256 凭据、`*.oss.json` 中转意图/清理状态以及归属记录，状态依次为 `uploading → cleaning → cleaned`。这些小型记录用于恢复和审计，不随中转对象一起删除。修改中转根前应先让旧任务清理收尾；不要手动删恢复记录。

### 本机删除范围与状态控制

| 数据 | 本机保留/清理规则 |
| --- | --- |
| 原始 ROSbag | 仅在“202 备份校验后释放本机原始 ROSbag”勾选，且质量校验、202 全文件 SHA256、完成凭据及台账持久化均通过后删除；备份失败保留。 |
| 清洗中间包、单包重复 dataset | 安全加入本地不可变 LeRobot 批次后，按已有中间产物清理规则释放；与原包删除开关分开。 |
| 完整 LeRobot 批次 | 默认保留。28 分区勾选“两版上传校验后释放本机完整 LeRobot”后，等待202完整备份、两版成品与配对凭据全部完成并再次远端全SHA校验，释放本批及旧版本的数据/视频；保留元数据、来源、封存和恢复记录。失败保留并重试。 |
| normal/speedup 成品 | 继续保留本机副本，两个释放开关均不删除这两版成品。 |
| 队列、紧凑审计及台账 | 持久保存，用于恢复、核验和查询。 |

“停止当前任务/停止全部任务”会等当前作业正常收尾；保存配置前需等相关工作流完全停止。“重试失败项”保留在进度框内，用于准备失败项重试，不清队列或历史成品。修改输入或上传目标可能使用独立运行版本，应保留旧版本恢复记录。

记录表展开时显示封存提示和“清空”；折叠时两者隐藏。“清空”仅隐藏当前任务当时已有的表格记录，新记录继续显示；不改变工作流、进度统计、实际日志文件或下方详细日志。自动刷新和任务切换不会恢复这些旧行，重新加载页面可恢复全部显示。

设置保存在 `outputs/gui/settings.json`，各任务状态、日志、批次与审计位于 `outputs/gui/runs/`。新安装或示例配置中的 `demo-input`、`.gui-validation-*` 都不是实际采摘输入；首次使用应选择真实场景目录并核对四个目标后保存。

推荐首次操作顺序：选择项目/手型 → 浏览本地场景目录 → 核对场景和四个上传父目录 → 核对 SSH 及连接测试 → 确定已有包接入和原包保留选项 → 保存 → 开始当前任务 → 查看记录和上传状态。

## 命令行兼容方案：本地清洗、24小时批次、normal/speedup与训练服务器上传

以下 YAML、SSHFS 和独立 systemd 服务说明对应继承的命令行方案，其个人 `workspace/data` 布局、批次旧命名和首次跳过历史等选项与上面的 GUI 配置分别管理。使用 GUI 时按上节选择服务器原生路径，具体以 GUI 保存设置为准。

分支名：`clean-covert-normal-speedup`，从 `Auto-convert-transfer-data` 继续开发。
原始rosbag备份、备份核验后的本机原始释放、完整24小时LeRobot向data服务器上传，均沿用已有链路。
新增独立本地后处理服务：只读取已经完整封存的本地24小时批次，生成normal和speedup，校验后将两版上传训练服务器。

```text
rosbag → 本地清洗/转换/校验
  ├─ 原始rosbag → data服务器（既有链路）
  └─ 本地24小时完整LeRobot封存
       ├─ 完整LeRobot → data服务器（既有链路）
       └─ 本地normal → 本地AdaTempo speedup → 两版均上传训练服务器
```

**手型与维度保持原样**：Sharpa为state116/action58，Wuji为state108/action54；保留关节名称、顺序及全部关节维度，不做跨手型映射。
normal只移除训练不使用的深度、触觉和额外审计数组，保留完整轨迹/帧及RGB视频字节；speedup保留全部轨迹，按1–3倍率范围进行时间重采样，视频分辨率及30Hz保持，重新编码视频并重算索引/统计。
记录的双臂engaged状态原样保留，不凭空填充双手engaged。

训练服务器输出明确区分机器人、A/B、批次及两个版本：

```text
/home/<个人目录>/workspace/data/
  sharpa/A/<批次名>--<批次ID>--<版本标识>/normal/
  sharpa/A/<批次名>--<批次ID>--<版本标识>/speedup/
  sharpa/B/...
  wuji/A/...
  wuji/B/...
```

`mengxiang`只是示例个人目录，其他人可使用`tairan`、`junyu`、`zhiwei`等；修改`training_target.dataset_root`为本人服务器上的`/home/<个人目录>/workspace/data`。
SSH登录账号`training_target.user`与个人目录分别配置，两者不必相同；程序自动追加`sharpa`或`wuji`及后续层级，不要在目标根里重复填写机器人名称。
支持SSH（例如`47.116.73.163:28`，使用已有SSH配置和密钥）或本地已挂载的训练服务器目录。data服务器的SSHFS配置与训练服务器的SSH上传配置互不替代。

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[conversion,training]'
cp configs/training.example.yaml configs/training.local.yaml
# 按本人环境修改capture_project、capture_config及training_target。
.venv/bin/python -m convest.training.cli check-config --config configs/training.local.yaml
# 先预览服务；加--apply才安装并启动新的独立用户服务。
.venv/bin/python scripts/install_training_service.py --config configs/training.local.yaml
.venv/bin/python scripts/install_training_service.py --config configs/training.local.yaml --apply
systemctl --user status capture-normal-speedup.service
.venv/bin/python -m convest.training.cli status --config configs/training.local.yaml
```

已有采集服务在另一目录运行时，`capture_project`填该采集工程的绝对路径，`capture_config`填原配置的绝对路径。
后处理从上游工程自身加载配置，保持其路径及指纹，不重建原队列、不重置24小时计时；后处理自身有独立状态、工作目录和上传队列。
只有本地批次catalog、匹配generation的封存校验和seal记录全部通过才处理；不等待向data服务器上传完成，也不处理尚在积累的批次。
训练端整对normal/speedup先暂存、完整SHA256回读校验后原子发布，目标冲突不覆盖；上传失败可续传，不重新加工。
两版均只上传，不自动启动训练；选择一个固定版本并使用对应数据重新计算训练norm_stats。详见[部署与语义说明](docs/normal_speedup.md)。

本次回归244项通过、7项跳过；真实Sharpa/Wuji各一条样本已完成本地两步加工、SSH上传及训练服务器全文件SHA256校验，原生维度保持。见[验收记录](docs/normal_speedup_validation.json)。24小时封存以受控试验时钟验证，不代表已运行满自然24小时。

服务器已有历史大包可使用一次性历史入口，在固定来源版本、完整副本哈希及保留列校验后，本地生成原生维度的normal/speedup再上传；不改变上游批次状态。适用条件、恢复与缓存复用方式见[历史批次处理说明](docs/normal_speedup.md#已有服务器历史批次的一次性处理)。

## 继承的采集电脑本地处理与排队上传

以下为原 `Auto-convert-transfer-data` 的采集功能，本分支完整保留：发现本机新生成的 rosbag，确认录制完成并通过稳定性及完整性检查后，在本机清洗、转换和校验，最后将原始 rosbag 与通过校验的 LeRobot 数据加入上传队列，传到data服务器对应目录。

```text
采集电脑录制 rosbag
  → 录制完成与输入检查
  → 本地静止段清洗
  → 本地 LeRobot 转换与完整校验
  → 持久上传队列
  → 服务器原始 rosbag / LeRobot 对应目录
```

本分支已新增采集端入口 `python -m convest.capture.cli`，包含独立本地处理与上传队列、断线重试、文件续传和上传回读 SHA-256 校验。四路真实样例已完成端到端试验：Sharpa A36/B55、Wuji A995/B1003，共4,155帧，原始rosbag与LeRobot均已上传并通过校验。见 [采集端使用说明](docs/capture_transfer.md) 和 [真实验收记录](docs/capture_validation.json)。下文服务器自动接收器属于原有功能；采集端使用本节的新配置和命令。

### 24小时常驻与存储释放（新模式）

使用 `configs/capture.daily.yaml`：四路各自从首包成功加入起累计24小时，到期完整校验并封存成一套LeRobot后排队上传；没有新包时也会到期封存，重启不会重置计时。原始rosbag在每包本地清洗、转换、来源/输出校验及本地批次安全提交后立即优先上传，不等24小时。

原始上传逐文件回读SHA-256、完成凭据及本机台账落盘后，自动删除工作机上对应原始包；未完成或校验不一致时保留。清洗中间包及单包数据集重复副本在本地不可变批次安全接收后释放。已上传LeRobot批次目前继续保留本机副本。

包号、类别、内容身份、原始上传及删除时间、所属批次、批次截止时间和远端位置实时保存到 `outputs/capture-daily/state/recordings.csv`、`recordings.json` 与SQLite事件日志；上传器另在服务器 `data-lerobot/.capture-ledgers/` 下备份台账。单个原始包也有独立服务器完成凭据，断线/进程中断后可恢复。

```bash
# 检查配置；不会删除或上传。
.venv/bin/python -m convest.capture.cli check-config --config configs/capture.daily.yaml

# 安装/启动当前用户的常驻服务（工作机本地，不操作服务器服务）。
.venv/bin/python scripts/install_capture_service.py --config configs/capture.daily.yaml --apply

systemctl --user status capture-convert-transfer.service
journalctl --user -u capture-convert-transfer.service -f
```

配置默认首次跳过已有历史包，持续接收之后新增的目录，不会因此回溯删除此前的样例。存储不足时暂停新转换并重试，不删除未确认备份的原始包。详见 [24小时模式说明](docs/capture_daily.md)。下面的 `capture.testing.yaml` 是上一轮按包成对上传的兼容试验模式，常驻使用上面的daily配置。

### 启动与查询

```bash
# 只检查配置，不启动或上传。
.venv/bin/python -m convest.capture.cli check-config --config configs/capture.testing.yaml

# 本次四路样例试验；处理与上传完成后自动退出。
.venv/bin/python -u -m convest.capture.cli run --config configs/capture.testing.yaml --until-idle

# 查询本地处理队列及上传状态。
.venv/bin/python -m convest.capture.cli status --config configs/capture.testing.yaml
```

持续接收新录制请复制 `configs/capture.example.yaml` 到自己的配置文件，修改输入、上传目标及独立的本地 `state_root` / `work_root` 后执行 `run`（不加 `--until-idle`）。不要同时启动两个使用同一状态目录的处理器；重复启动会被锁拒绝。已有试验正在运行时先查询状态，不要另开重复试验。

### 部署到其他工作机：必须检查的输入和输出路径

先复制24小时配置，再按实际电脑和服务器修改；下面的路径仅是当前试验示例，不能在其他工作机上直接照用。

```bash
cp configs/capture.daily.yaml configs/capture.local.yaml
```

| 配置字段 | 用途及修改要求 |
| --- | --- |
| `routes[].source` | 工作机录制 rosbag 的输入目录；四路分别填写，目录下直接放 `episode数字/`，不要填成某一个 episode。用 `robot` 和 `group` 明确对应 Sharpa/Wuji、A/B。 |
| `remote_raw_root` | 原始 rosbag 的服务器目标根目录；程序自动追加 `{robot}/{group}/{episode}`，根目录不要再填写 `wuji/A` 等路由后缀。 |
| `remote_dataset_root` | LeRobot 的服务器目标根目录；程序自动追加 `{robot}/{group}/{批次名}`。 |
| `require_mount` | 工作机上实际的服务器挂载点，例如 `/home/user/company-remote-2`；两个远端根目录必须位于此挂载点内。未挂载时上传等待，不会把数据写入同名本地空目录。 |
| `state_root` | 本机持久队列、处理台账和上传状态，例如 `outputs/capture-local/state`。 |
| `work_root` | 本机清洗和转换临时目录，例如 `outputs/capture-local/work`。 |
| `local_batch_root` | 本机24小时 LeRobot 批次目录，例如 `outputs/capture-local/batches`；不是服务器上传目标。 |

三个本机目录必须分别位于工程的 `outputs/` 下，互不包含，也不能与输入或远端目录重叠。相对路径按工程根目录解析，与终端当前目录无关。

本节命令行采集方案使用**工作机已挂载的服务器文件系统**（例如SSHFS），不直接读取 `user@host:/path` 地址。先将服务器目录挂载到工作机，再填写挂载后的路径；服务器自身的 `/data/...` 路径不能直接当作工作机路径使用。服务器上的两个目标根目录需要预先存在且可写。

首次部署时，`initial_history: {mode: none}` 会登记并跳过当时已有的包，仅接入之后出现的新包；若需要处理已有录制，应在首次启动前改为 `initial_history: {mode: all}`。`delete_raw_after_upload: true` 表示原包在服务器完整备份、校验和凭据落盘后自动从本机删除；希望保留本机原包则设为 `false`。

修改完成后检查并使用**同一份配置**安装服务：

```bash
.venv/bin/python -m convest.capture.cli check-config --config configs/capture.local.yaml
.venv/bin/python scripts/install_capture_service.py --config configs/capture.local.yaml --apply
```

已有服务运行时，先执行 `systemctl --user stop capture-convert-transfer.service`，等待当前处理和上传正常结束，再修改配置。相同配置继续使用原队列；更换输入、上传目标或历史接入策略等身份配置时，必须同时选择新的 `state_root`、`work_root`、`local_batch_root`，保留旧目录供查询和恢复。新队列不会继承旧队列的去重记录，先核对旧队列及目标数据，避免重复接入或同名冲突。

#### 当前试验的输入路径

| 路由 | 采集电脑 rosbag 存储目录 |
| --- | --- |
| Sharpa A | `/home/user/franka_teleop_data/bags/gello_sharpa_tactile/A` |
| Sharpa B | `/home/user/franka_teleop_data/bags/gello_sharpa_tactile/B` |
| Wuji A | `/home/user/franka_teleop_data/bags/gello_wuji/A` |
| Wuji B | `/home/user/franka_teleop_data/bags/gello_wuji/B` |

这四条路径作为当前开发试验输入；目录出现不等于录制完成，不能处理仍在写入的包。各路输入目录通过配置文件的 `routes[].source` 修改，支持在不同采集电脑上替换。

#### 当前试验的服务器上传位置

以下是采集电脑通过服务器挂载访问的目标路径：

| 上传内容 | 试验目标根目录 |
| --- | --- |
| 原始 rosbag | `/home/user/company-remote-2/real/harvest-tomato/data/zmx-testing/origin-rosbag` |
| 已通过本地校验的 LeRobot | `/home/user/company-remote-2/real/harvest-tomato/data/zmx-testing/data-lerobot` |

两个根目录均按 `sharpa/A`、`sharpa/B`、`wuji/A`、`wuji/B` 分开保存。例如 Sharpa A 原始包上传到 `origin-rosbag/sharpa/A/`，其 LeRobot 上传到 `data-lerobot/sharpa/A/`；Wuji B 对应 `origin-rosbag/wuji/B/` 和 `data-lerobot/wuji/B/`。

上传的 rosbag 是采集得到的原始包；清洗后的 rosbag 属于本地处理中间产物，不代替原始包。上传目标根目录分别通过 `remote_raw_root` 和 `remote_dataset_root` 配置，路由由各项 `robot` / `group` 决定；本次 `zmx-testing` 路径没有写死在程序中。这些路径是本机挂载路径，服务器原生路径或其他传输连接参数需在选定传输方式时另行配置。

24小时批次命名为 `最小来源包号_最大来源包号_data_lerobot`（例如 `23_50_data_lerobot`），允许缺号，实际来源见台账。四路各自从首包成功加入起计时，不是每天固定零点上传。传输先写目标路由下的 `.uploading/<任务ID>/`，全部文件清单、大小和回读SHA-256通过后，才在同一文件系统内重命名为正式批次目录。下游只读取 `*_data_lerobot` 正式目录，忽略 `.uploading`、`.capture-transfer`、`.capture-ledgers` 等内部目录，避免读取上传中的数据。原始rosbag也使用暂存后发布机制。

### 运行边界

- `main` 用于服务器侧处理，本分支用于采集电脑侧处理；两者独立部署。安装工作机服务不操作服务器生产服务、配置、队列或 NVMe 挂载，服务器是否运行由服务器自身维护状态决定。
- 本地处理和上传分别记录状态。上传队列应支持重启恢复、失败重试和去重；传输中断不能要求重新清洗、转换已经验证完成的数据。
- 本地沿用现有输入检查、裁剪规则、转换与来源及输出校验，不为上传提速降低质量门槛。未通过校验的输出不能作为合格 LeRobot 上传。
- 上传完成需核验内容完整性，再标记成功；未完成的传输不能作为已就绪数据暴露。原始 rosbag 和 LeRobot 的上传状态分别记录，并关联同一来源。
- 单包兼容试验模式保留本机原始与中间文件；当前推荐的daily模式执行原始备份后释放、24小时批次封存上传及台账存底。历史接入通过 `initial_history` 配置，传输使用已挂载的服务器目录。

默认按图像/深度/触觉图像的 Header 拍摄或采样时间判断裁剪归属，静止区间和质量阈值保持原规则。已成功的 LeRobot 不重转；仅对 Wuji 因内部对齐缺口被排除的包定向补缺。升级保持原队列、批次计时及暂停状态，详见 [拍摄时间修复与历史数据补缺](docs/capture_time_upgrade.md)。

## 支持范围

| 机器人 | State / Action 维度 | 输出模态 |
| --- | --- | --- |
| 双 FR3 + Wuji | 108 / 54 | 三路 RGB、头部 uint16 深度、双臂双手状态与动作、接管状态 |
| 双 FR3 + Sharpa | 116 / 58 | 三路 RGB、头部 uint16 深度、双臂双手状态与动作、双臂接管状态、双手十指形变图及六维力/力矩 |

输入为未使用 bag 层压缩的 ROS2 SQLite3 bag，支持分卷；消息时间字段改写支持 CDR1。关节按名称映射，动作保存为绝对关节目标，单位为弧度。消息与关节契约见 [schemas/](schemas/)。

转换需要有效的采集验证状态：原始包提供 `collection_state.json`；本工具清洗后的包提供 `source_collection_state.json` 和 `trim_report.json`。本地 job 要求采集状态为 finalized、验证失败列表为空，且文件完整、消息计数一致。批处理命令应在采集和传输结束后运行；自动接收模式可以提前启动，持续扫描配置的输入目录，执行稳定观察、完整性和录制质量检查。

暂不支持 ROS1、MCAP、压缩 bag 或任意机器人话题布局。Sharpa 手部未记录接管状态时，使用新鲜的绝对命令，不构造手部接管字段。

## 命令行版本安装

需要 Python 3.11+。转换不依赖 ROS 安装；生成预览另需 PATH 中可用的 `ffmpeg` 和 `ffprobe`。

```bash
git clone --branch clean-covert-normal-speedup ssh://git@gitlab.litchibot.top:2222/fruit-infra/data-auto-processing.git
cd data-auto-processing
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[conversion,preview]'
```

仅使用 bag 裁剪功能时，可使用 `pip install -e .`。开发测试需额外安装 `test` 依赖。

## 配置

复制对应的机器人配置，再修改输入路径和数据集信息：

```bash
cp configs/wuji.yaml configs/my_wuji.yaml
# Sharpa 使用 configs/sharpa.yaml 作为模板。
```

| 字段 | 用途 |
| --- | --- |
| `source_root` | 对应机器人的原始 bag 根目录 |
| `output_root` | 独立转换时的默认输出目录 |
| `repo_id` | 数据集标识，格式为 `namespace/dataset`；不代表自动上传 |
| `task` | 当前采集任务的语言描述，需按实际任务修改 |
| `contract` / `schema_dir` | 关节、话题契约及自定义消息定义 |

相对路径以项目根目录为基准解析。转换输出必须位于项目内；job 输出必须位于 `outputs/` 下。Wuji 和 Sharpa 应分别配置、分别输出，避免混入同一数据集。

## 自动接收新上传的数据

接收器只监控配置的 Sharpa/Wuji A/B 输入目录，自动发现 `episode数字` 目录并登记队列，不依赖上传器实现、人工标记或回调。新 episode 出现后，先按默认 10 秒扫描、3 次一致且稳定 60 秒观察，再检查归档状态、metadata 所列分卷、SQLite、消息数和必需数据流；目录出现本身不代表已经完整。`.uploading/` 等不匹配 episode 名称的目录不会被递归扫描，不要求上传方使用这些目录。

首次启动必须显式配置 `initial_history`：服务器模板设为 `all`，接收已有包和未来新包；本地模板仍为 `none`，跳过首次扫描时已有的包，也支持指定历史清单。该范围持久保存，重启恢复队列并补查遗漏。通过体检后执行既定“清洗 rosbag → LeRobot v2.1 → 校验”，原始包只读，源变化时丢弃本次尝试并重试。

四条路由分别输出到 `data-lerobot/{sharpa,wuji}/{A,B}/`。每批从第一个包成功追加起计时，默认 72 小时；批内持续追加，到期后通过全批校验封存，后续成功包进入下一批。每套完整数据集命名为 `起始原始包序号_结束原始包序号_data_lerobot`，例如 `36_68_data_lerobot`；序号范围允许缺号，实际纳入清单和封存状态可用 `ingest-worker status` 查看。活动批名称随范围更新，封存后固定；每批 LeRobot 内部编号从 0 开始，已处理来源跨批去重。没有下一条时仍会自动处理和封存，不生成空批次。

```bash
python -m convest.ingest.cli check-config --config configs/ingest.local.yaml
python -m convest.ingest.cli run --config configs/ingest.local.yaml
```

本地模板仅指向项目 `outputs/`；服务和服务器配置只是模板，未自动部署。接收检查、首次历史范围、异常源变化及数据集版本发布说明见 [自动接收说明](docs/automatic_ingestion.md)。

接收体检每次尝试完整执行一次。服务器模板使用 `copy_input: false`：直接只读原始 bag，清洗结果另存临时目录，省去原始副本复制及其读回校验；原始包不删除、不移动、不改写。全过程保留变化检测和最终源文件哈希，输出来源对比、视频、深度和触觉校验不变。`copy_input: true` 保留副本模式，旧配置未填写时仍默认复制。`status` 展示细分阶段及哈希字节进度；`drain` 可在当前包发布并清理后进入维护，`resume` 恢复原队列。检查复用边界和安全升级见上述说明。

直接读取的自动接收流程将源文件最终哈希统一放在发布前执行，省去 job 内对同一原始 bag 的一次重复整文件读取。job 在输出校验通过后先记录 `awaiting_source_verification`，不声明完成；接收器完整复核所有原始文件内容、快照及契约后才记录完成并发布。独立 job 和复制模式仍保留原有最终哈希；视频、来源数值、发布副本及追加验证不变。

后续服务器路径、依赖、首次启动范围和验收步骤见 [服务器部署准备](docs/server_deployment.md)。

## 转换前体检与清单

可以先检查输入，不生成清洗 bag 或数据集：

```bash
convert-worker inspect --config configs/my_wuji.yaml \
  --report outputs/reports/input_check.json

# 只检查清单中的来源；存在未就绪项时返回码为 2。
convert-worker check-list lists/my_dataset.txt --config configs/my_wuji.yaml \
  --report outputs/reports/list_check.json
```

体检检查采集状态、分卷、SQLite、消息计数、SHA-256、必需话题、自定义消息及命名关节。报告给出每路时间索引去重后的样本数、中位频率、时间间隔中位数/P99/最大值，以及 RGB、深度、触觉图的端点编码与尺寸。体检会读取源文件计算哈希；图像检查覆盖首尾样本，不替代正式转换中的全部选中帧解码和连续性校验。

`--report` 可省略；指定时必须是项目 `outputs/` 下的新文件，不能位于输入目录内，也不覆盖已有报告。原始包尚未传输完成或缺少 metadata 的 episode 目录会列为未就绪，不会静默漏掉。

TXT 清单支持数字、`episode1`、连续范围 `1-3`、空白或中英文逗号分隔以及 `#` 注释。重复、反向范围和缺失来源会报错：

```text
# 手动选择的同一机器人、同一任务数据
1-3
8
```

体检面向原始采集目录；已有清洗包在独立转换时通过保留的采集状态和裁剪报告校验。体检与 job 都支持 `--episode-list`；`--episode` 与 `--episode-list` 互斥。清单只是人工选择，不代表自动判断任务成功或数据质量等级。

## 完整工作流

```bash
# 检查、裁剪、转换并校验配置目录下的全部 episode。
convert-worker run --config configs/my_wuji.yaml \
  --run-root outputs/jobs/my_dataset

# 中断恢复、重试失败任务，或处理新增的包。
convert-worker run --config configs/my_wuji.yaml \
  --run-root outputs/jobs/my_dataset --resume

# 按清单执行完整链路；后续追加清单后用同一路径和 --resume。
convert-worker run --config configs/my_wuji.yaml \
  --run-root outputs/jobs/selected_dataset --episode-list lists/my_dataset.txt

# 仅处理指定包；--episode 可重复传入。
convert-worker run --config configs/my_wuji.yaml \
  --run-root outputs/jobs/subset --episode episode1
```

`--workers` 支持 1–4，默认 1，控制同时转换的 bag 数。每个来源独立暂存，按确定顺序提交。文件锁阻止同一任务或数据集被并发写入。

`--resume` 校验配置与来源，复用已完成的裁剪、跳过已提交转换，并重新执行校验。它不会删除历史已提交数据。更换任务、契约或转换参数时应使用新的输出目录。

任务状态和失败原因保存在 `jobs/` 与 `summary.json`。已提交转换若验证失败，记录为 `verification_failed` 并保留 `conversion_committed: true`；其他通过验证的来源仍可标记完成。数据集级验证失败会返回非零，不会把部分通过误报为整批通过。本节命令为本地批处理；常驻扫描、持久队列和接收检查见上面的自动接收说明。

## 独立运行

```bash
# 只分析静止区间。
bag-worker inspect /data/bags/episode1 --report outputs/inspection.json

# 裁剪单包或批量裁剪；输出目录必须不存在。
bag-worker trim /data/bags/episode1 --output outputs/cleaned_bag
bag-worker batch /data/bags --output outputs/cleaned_batch

# 为清洗后的 bag 生成三路 RGB 预览。
python scripts/preview_batch.py outputs/cleaned_batch

# 将已有清洗包转换为 LeRobot，不再次裁剪。
convert-worker convert --config configs/my_wuji.yaml \
  --source-root outputs/cleaned_batch --output outputs/lerobot/my_dataset

# 仅校验数据集自身，可在没有原始包、原配置路径的机器上运行。
convert-worker verify outputs/lerobot/my_dataset

# 另外启用独立来源审计，需要来源 bag、契约及消息定义可访问。
convert-worker verify outputs/lerobot/my_dataset --compare-source
```

独立裁剪可通过 `--config configs/idle.yaml` 指定检测参数；串联 job 使用内置默认裁剪参数。裁剪输出不能位于输入目录内，已裁剪的包会被拒绝再次裁剪。转换续跑使用 `--resume`，不能将任意非空目录作为已有数据集接管。

## 静止段裁剪

裁剪使用左右臂各 7 个实测关节，逐消息按关节名称对齐。以较慢状态流的原生记录时间为参考，匹配另一臂最近的过去状态；默认允许的状态年龄为较慢流中位周期的 1.5 倍。

默认在前后合计 100 ms 的完整窗口内，每个关节的最大值与最小值之差不超过 `0.0001 rad` 时，标记为静止。候选计数要求超过 `stationary_frames=3`，该计数包含进入边界帧；删除区间为 `[times[start+1], times[i-1])`，保留边界状态。参数见 [configs/idle.yaml](configs/idle.yaml)。

仅删除前后均观察到运动的内部静止区间。录制首尾保留；非有限值、配对状态过旧、超过中位周期 3 倍的状态断流及不完整窗口会中断静止证据。遇到这类边界时，分别保留并裁剪两侧已经完整观测到的静止子段，不丢弃之前积累的静止证据，也不跨越不确定区间合并裁剪。每个子段保留边界状态。手部运动不参与裁剪判定，不另外合并相邻区间。

检测版本 3 增加独立的静止块覆盖检查：直接检查每个完整静止块的安全内部是否已覆盖，并检查删除区间未穿过运动或无效帧；检查失败即停止导出。结果写入 `auto_idle_detection.effectiveness_audit`。该检查用于验证既定规则是否执行完整，不代表识别任务阶段或证明任务成功。

RGB、深度、触觉、关节和控制消息等全部话题使用共同的删除区间，并同步压缩时间轴。这里删除的是主动识别的等待，与转换阶段遇到缺失数据时的处理不同。

## 时间轴、帧率与采样方法

### 有效范围与时间戳

转换使用采集报告的 `source_effective_start_time_ns` 到 `source_effective_end_time_ns`。清洗后的采集报告在内存中映射到压缩时间轴；不重复执行固定时长的首尾裁剪，也不扩展到整个 bag 的起止范围。选中范围最外侧不满足有效性要求的采样点仍可能被裁掉。

默认以消息 `header.stamp` 建立整数纳秒时间索引。到达乱序的消息按源时间稳定排序，相同时间保留索引遍历顺序中的最后一条。标准消息在清洗时已经改写过的时间戳不再次映射。

未知自定义类型在裁剪时保留消息体，只改写 bag 记录时间，并列在 `trim_report.json` 的 `unmodified_payload_types` 中。转换时：

- 被标记为未改写的 `ArmCommandStatus`、`HandTelemetryStatus` 使用压缩后的 bag 记录时间。
- 其他被标记为未改写的类型，其 header 时间通过裁剪映射转换。
- 未被标记的消息继续使用 header 时间。

零 header、缺少必需流、错误序列化、重复或缺失关节、缺少速度及非有限关节值会报错，不用零值伪造状态。不要把 `source_collection_state.json` 直接改名为 `collection_state.json`，否则会混用两条时间轴。

### 原始频率不同，怎样统一到 30 Hz

当前转换目标固定为 **30 Hz**，在有效时间窗口内建立等间隔采样网格。每个时刻、每路数据分别选择时间不晚于该时刻的最近消息，即因果采样保持（零阶保持，ZOH）：

```text
原始图像时间：     0 ms          50 ms         100 ms
原始图像：          A              B              C
输出网格：        0 ms    33.3 ms    66.7 ms    100 ms
输出使用：          A        A          B          C
```

这说明 20 Hz 相机在转换为 30 Hz 时会自然重复部分图像；200 Hz 关节状态则只选取网格时刻可用的过去样本。深度与触觉也分别按自身时间戳采样，不按消息序号与 RGB 强行配对。

**重复使用过去帧不是生成新观测，也不一定表示源数据丢帧。** 程序不取未来帧、不做线性插值、光流补帧或神经网络补帧。统一输出帧率也不会校准设备时钟偏移。

每路 RGB 保留原分辨率，使用 H264/yuv420p，默认 CRF 23、`veryfast`。三路输出视频均为 30 fps，第一帧 PTS=0；Parquet 第 i 行与各路视频第 i 帧对应。视频有损编码不保证 RGB 像素逐位相同；深度和触觉数值不经过该视频编码路径。

### 状态、动作与接管

状态和动作分别读取实测关节与命令，不把实测位置直接当作正常接管时的动作。动作保持绝对关节目标，不预先计算手臂 delta。

有实际接管状态的分组，接管时要求命令新鲜；明确停用后保持停用瞬间可用的最后目标。录制开始即停用且没有历史目标时，使用实测位置初始化。接管状态本身仍需新鲜、有效；重新接管要求匹配新命令，允许状态与命令最多 20 ms 的发布偏差。

因此，“停用后保持目标”不要求该历史命令一直满足 150 ms 的年龄限制，但不是允许接管状态或实测状态断流。Sharpa 手部没有实际接管流，不使用这种停用推断；其绝对命令必须持续满足新鲜度要求。

## 缺帧、过期与有限保持

### 默认阈值

```yaml
fps: 30
max_staleness_ms: 150
camera_short_gap_max_frames: 2
camera_short_gap_max_age_ms: 250
min_segment_frames: 2
```

| 参数 | 实际含义 |
| --- | --- |
| `fps` | 本目标固定为 30，所有模态共用输出网格 |
| `max_staleness_ms` | 正常采样允许的最大消息年龄；适用于实测状态、有效接管状态、接管中的命令、RGB、深度及触觉 |
| `camera_short_gap_max_frames` | 每处内部视觉缺口可接受的连续输出采样点数，支持 0、1、2；0 关闭 RGB 与深度接缝的有限保持 |
| `camera_short_gap_max_age_ms` | 有限保持时允许的最大实际图像年龄，不能靠改时间戳满足此条件 |
| `min_segment_frames` | 选中有效 episode 的最短帧数，不是动作窗口长度，也不能消除训练端的末尾 padding |

“消息年龄”是当前网格时间减去被选中消息的采样时间。没有过去样本、消息过期、接管状态无效或活动命令不满足条件，都会让相应采样点无效。

### RGB 怎样处理短缺口

允许继续使用已经选中的过去图像，但必须同时满足：

1. 缺口位于窗口内部，不是首尾不完整区间。
2. 按三路 RGB 无效点的**并集**计算，连续缺口不超过两个输出采样点。
3. 缺口及其相邻边界的低维状态、动作、接管状态均有效。
4. 缺口内每路 RGB 都有过去图像，且实际年龄不超过 250 ms。

“两帧”指 30 Hz 输出网格上的两个点，两个采样周期合计约 66.7 ms，不是允许源相机任意丢两帧，也不是允许图像额外老化 250 ms。250 ms 是图像的总年龄上限。

多处独立短缺口分别检查，例如分别缺 1、1、2 个点可以逐处容忍；多路相机交错形成的长缺口不能拆开规避限制。连续 3 个无效点不会只修前两个；两点以内但图像太旧、没有过去图像或低维数据无效，也不能保持。

这里只允许视觉模态继续引用原样本，不复制整条观测与动作记录，不修改图像原始时间。

### 深度与触觉的处理

普通深度和触觉遵守 150 ms 的严格年龄限制。深度有一个只适用于本工具裁剪接缝的例外：

- 深度缺口为内部区间，连续点数不超过 `camera_short_gap_max_frames`。
- 缺口及相邻网格范围内存在 `trim_report.json` 记录的实际接缝。
- 缺口内各点同时属于已经接受的头部 RGB 短缺口。
- 其他模态及相邻边界均有效；所选深度图来自过去，实际年龄不超过 `camera_short_gap_max_age_ms`。

满足条件时继续使用原深度图，记录每个点的真实图像年龄、源时间及接缝位置。没有清洗映射的原包、不在接缝附近的深度缺口、仅深度异常而 RGB 正常的情况，均不使用此例外。

RGB 与深度曝光时间不要求完全一致：已接受的头部 RGB 短缺口与深度短缺口有交集即可，不要求深度缺口的每一帧都落在 RGB 缺口内。其余限制仍同时适用：必须紧邻已记录的裁剪接缝、最多 2 个输出点、实际样本年龄不超过 250 ms、两侧边界及其他模态有效；报告会记录交集帧数和实际样本年龄。

触觉形变图、力/力矩不使用视觉宽限。它们仍可在正常 150 ms 范围内因果保持；超过范围就无效。深度零值原样保存，不代表程序已经重建了缺失距离。

### 仍有缺口时怎样处理

有限保持后，只允许裁掉选中范围最外侧的无效网格点。两个有效部分之间仍有任意内部无效点时，整条录制记为质量排除；本目标不会拆成多个 `part_N`，也不会删除未知缺失区间后把两侧拼成相邻训练帧。

一条合格录制生成一条 episode。当前仅支持 `segments: full-all`，不提供阶段识别或四阶段导出。质量排除与程序失败分别记录；命令正常结束也可能存在质量排除，应检查汇总报告。

## 怎样查看缺帧及保持情况

`inspect` 的报告描述转换前的来源时间索引；完整 job 的总报告位于 job 根目录的 `quality_report.json`，包含原始时长与消息数、删除时长/消息数及其比例，并关联输入频率、转换和验证指标。独立转换的数据集报告位于 `dataset/conversion/quality_report.json`。其中汇总对齐无效点、实际导出帧数、各流最大接受年龄、重复采样比例、采样年龄 P50/P99/最大值、有限保持、质量排除和验证失败。重复比例只读取小尺寸审计列，不加载整批深度或触觉数组。停用命令允许保持历史目标，其采样年龄应结合接管状态理解。

新转换来源记录还保存 `cleaning`，包含删除时长、删除消息数及时间映射；已有记录缺少该项时显示为未记录，不把缺失值当作零。程序记录对齐网格上的无效点、各流未匹配点和有限保持情况，**目前没有独立的源相机丢帧率或网络丢包率统计**。应区分以下口径：

| 指标 | 含义与限制 |
| --- | --- |
| 源丢帧率 | 需要来源的期望频率、序列号及采集契约；不能仅用导出帧数反推，当前转换报告不直接给出 |
| 对齐无效率 | `invalid_frames / grid_frames`；统计整个有效窗口在保持处理后仍无效的输出点，包含可能被裁掉的首尾，不等于源丢帧率 |
| 单流未匹配比例 | `unmatched[流名] / grid_frames`；是该流检查后的网格计数，多路可能重叠，不能直接相加；状态有效标志等另有检查，不能单凭该值判断整行有效 |
| RGB 保持点数 | `camera_short_gap_tolerance.accepted_frames`；按缺口并集计数，指超过正常新鲜度后被有限接受的点，不含正常升帧造成的所有重复图像 |
| 深度接缝保持点数 | `depth_splice_tolerance.accepted_frames`；只统计满足接缝例外的深度点 |
| 导出重复比例 | 可按同一路连续输出使用相同源采样时间的比例计算；低帧率升到 30 Hz 本身就会产生重复，不能直接视为丢帧 |

成功提交来源的 `dataset/conversion/records/*.json` 中，`alignment` 包含：

- `grid_frames`、`invalid_frames`、`retained_frames`、`segment_count`：整个 validated 对齐窗口的统计。
- `unmatched`、`max_age_ms`：各流的检查计数及通过该流年龄检查的样本最大年龄；包含视觉宽限接受的年龄，不仅统计最终导出点。
- `camera_short_gap_tolerance.gaps`：RGB 缺口边界、涉及的相机、接受点、图像源时间与实际年龄。
- `depth_splice_tolerance.gaps`：深度接缝保持的网格范围、接缝、源时间及实际年龄。
- `selected_export`：真正导出的帧范围与帧数。对齐窗口统计不能直接当作最终导出统计。
- `splice_audit`：接缝前后各关节组的位置与动作变化，仅报告，不反向改写裁剪结果。

被质量排除的来源没有成功提交的 episode 记录；其缺口位置和长度在 `dataset/conversion/rejections.json` 中。程序异常位于 `errors.json`。转换汇总的 `excluded_by_quality`、`errors` 与 job 根目录的 `summary.json` 应分别检查。

例如查看已有数据集的对齐统计，不读取视频或大尺寸传感器数组。缺少的可选统计字段表示未记录，示例输出 `None`，不能按零次保持解释：

```bash
python - outputs/jobs/my_dataset/dataset <<'PYCODE'
from pathlib import Path
import json
import sys

root = Path(sys.argv[1])
for path in sorted((root / "conversion/records").glob("*.json")):
    record = json.loads(path.read_text())
    a = record["alignment"]
    n = a["grid_frames"]
    print({
        "source": record["source"],
        "grid_frames": n,
        "invalid_grid_ratio": a["invalid_frames"] / n if n else None,
        "exported_frames": a["selected_export"]["frames"],
        "rgb_tolerated_points": a["camera_short_gap_tolerance"]["accepted_frames"],
        "depth_splice_tolerated_points": a.get("depth_splice_tolerance", {}).get("accepted_frames"),
    })
PYCODE
```

普通升帧产生的重复可通过 Parquet 的 `audit.sample_timestamp_ns` 检查：按 metadata 中该列的 `names` 找到相机或传感器列，统计相邻行使用相同非零时间的次数，再除以相邻行对数。该列保存当前采样时间索引，不是原始包序列号；有重复 header 或裁剪时间被钳制时，也不能据此推断唯一的原始消息数量。

## 输出

```text
outputs/jobs/my_dataset/
├── manifest.json                 # 任务配置与指纹
├── jobs/                         # 各来源状态与完整性记录
├── cleaned/                      # 清洗后的 bag 与裁剪报告
├── dataset/
│   ├── meta/                     # LeRobot v2.1 元数据与统计
│   ├── data/chunk-000/           # 每 episode 的 Parquet
│   ├── videos/chunk-000/         # 三路 RGB MP4
│   └── conversion/               # 来源、时间对齐、提交与验证记录
└── summary.json
```

`dataset/meta/` 包含 `info.json`、`tasks.jsonl`、`episodes.jsonl` 和 `episodes_stats.jsonl`。每 1000 条 episode 一个 chunk，表格位于 `data/chunk-*/episode_*.parquet`，RGB 视频位于 `videos/chunk-*/observation.images.cam*/episode_*.mp4`。

`conversion/manifest.json` 保存转换配置与契约指纹；`records/` 是每个已提交来源的记录；`verification.json` 保存数值与视频校验结果，独立 loader 检查另写 `loader_verification.json`。这些记录与标准 LeRobot 元数据共同保留，便于恢复和来源审计。

RGB 使用 H264 有损编码；深度与触觉使用 Parquet 原生定长数值数组，流式分批写出：

| 特征 | 内容 |
| --- | --- |
| `observation.state` / `action` | 关节状态与绝对动作 |
| `observation.images.cam0` / `cam1` / `cam2` | 头部、左腕、右腕 RGB |
| `observation.depths.cam0` | 原始 uint16 深度 |
| `observation.tactile.{side}.{finger}.deformation` | 原始 mono8 形变图，仅 Sharpa |
| `observation.tactile.{side}.{finger}.wrench` | float64 力 xyz、力矩 xyz，仅 Sharpa |
| `source_timestamp_ns` | 当前时间轴上的采样时间 |
| `audit.original_timestamp_ns` | 映射到原始录制时间轴的网格时间 |
| `audit.sample_timestamp_ns` / `audit.receive_timestamp_ns` | 各流所选消息的采样时间与记录时间 |

深度零值不填补，深度比例与触觉标定不推断，相机内参随来源记录保存。接缝处的原始网格时间选择删除区间之后的分支。力/力矩按轴统计，深度及形变图使用全局标量统计，具体方法写入特征说明。

### 关节顺序与统计

State 与 action 均为 float32。State 按 `[左臂位置、左臂速度、右臂位置、右臂速度、左手位置、左手速度、右手位置、右手速度]` 排列；action 按 `[左臂、右臂、左手、右手]` 排列。双臂各 7 维，Wuji 每手 20 维，Sharpa 每手 22 维。具体关节名写入 `meta/info.json` 的 feature names。

Wuji 的 `observation.engaged` 为左臂、右臂、左手、右手四个实际接管状态；两种机器人都提供 `observation.arm_engaged`。Sharpa 不输出不存在的手部接管字段。

`meta/episodes_stats.jsonl` 保存每 episode 的 min、max、mean、std、count。关节向量及力/力矩按维度统计；深度和形变图使用可广播的全局标量统计。RGB 每段每路最多均匀抽取 100 帧，并进行空间下采样，按 [0,1] 通道值统计，不是全量像素遍历。

这些数据集统计不等同于训练端的 normalization stats。训练若进行关节重排、手臂 delta 或动作窗口变换，需要按相应训练配置另行计算。新增深度和触觉特征也需要训练端配置对应模型输入；生成数据集不会自动修改模型或启动训练。

### 动作窗口与 episode 边界

输出按 episode 独立保存，不把不同录制拼成一条时间序列。附带的 loader 检查使用 50 步 action 窗口，并检查 episode 尾部重复最后一个动作及 `action_is_pad` 标记。实际训练的窗口长度和 padding loss/mask 处理由训练端决定，不由转换器修改。

30 Hz 下，50 个动作采样点从第一个到最后一个相隔约 1.63 秒。将 `min_segment_frames` 设为 50 不能消除 episode 最后若干起点的 padding；训练侧仍需明确处理。训练/验证划分应按 `source_recording_id` 分组，避免同一来源的衍生版本进入不同集合。

## 校验范围与失败定位

默认 `verify` 只依赖本工具输出目录中的数据、metadata 和提交记录，不检查原始包或原机器上的契约路径，可用于搬迁后的离线结构校验。它检查每条 episode 的字段、嵌套数组形状、dtype、NaN/inf、索引、30 Hz 时间网格、统计、完整视频解码及逐帧 PTS。RGB 有损编码，不做源像素逐位相等断言。

`--compare-source` 额外启用逐时刻的独立采样审计：复用只读消息解析和关节契约，但不调用转换阶段的 `align` / `prepare`。重新推导状态、动作、接管保持、RGB 短缺口及深度接缝条件，再逐行对比关节、无损传感器数组和审计时间。该检查既能发现写出错误，也能检出转换对齐函数输出与约定不符的情况。

验证中每条 episode 单独记录结果，一条损坏不会阻止检查其余 episode：

| 文件 | 内容 |
| --- | --- |
| `conversion/verification.json` | 数据集总结果；运行中为 `running`，完成后为 `passed` 或 `failed`，包含逐 episode 结果 |
| `conversion/verification/episode_*.json` | 每条已提交 episode 的验证结果与错误 |
| job 根目录 `quality_report.json` | 整条链路的输入、裁剪比例、转换及验证汇总 |
| `conversion/quality_report.json` | 数据集质量指标、排除原因和验证错误；使用对应验证的完成时间标识 |
| `conversion/rejections.json` | 因内部缺口或最短长度条件而排除的来源 |
| `conversion/errors.json` | 转换程序错误 |
| `jobs/*.json`、job 根目录 `summary.json` | 裁剪/转换/验证状态及原始数据完整性结果 |

`convert-worker verify` 的关键验证使用显式异常检查，使用 `python -O` 也不会关闭校验。验证和转换共用数据集文件锁；验证报告需要输出目录可写。`--resume` 可重新校验已提交文件，但不自动覆盖或修复损坏的训练文件，数据内容损坏应定位后重新生成。

## 测试与读取检查

```bash
pip install -e '.[test,conversion,preview]'
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q
```

默认运行合成数据测试；需要本地真实样本的回归测试按环境条件跳过。输出校验不能替代对实际动作和裁剪效果的人工确认。

如已有安装 LeRobot v2.1 的独立环境，可检查标准 loader、传感器值及动作窗口：

```bash
/path/to/lerobot-env/bin/python scripts/check_lerobot_loader.py \
  outputs/jobs/my_dataset/dataset
```

该检查使用 PyAV，缓存写入项目内，不修改训练环境。来源与消息定义许可见 [移植记录](docs/conversion_provenance.json) 和 [消息定义许可](schemas/LICENSE.gello-retarget)。

## π0.5 的 RGB 训练兼容性

深度和触觉可以保留在数据集中，由训练适配明确选择三路 RGB、实测关节位置、动作和任务文本。额外列不会自动变成模型输入；标准 LeRobot loader 仍可能读取这些列，因此存储完整性和读取性能是不同问题。

Wuji 的 108D 原始状态和 54D 动作可使用现有 Wuji 适配。Sharpa 的原始状态为 116D 位置/速度，动作为 58D 的“左臂、右臂、左手、右手”，不能直接套用只接受物理顺序 58D 状态的旧训练配置。`tomato-openpi` （https://gitlab.litchibot.top/fruit-train/tomato-openpi.git）的独立配置 `training_configs.experiments.tomato_sharpa.multimodal_rgb.tomato_fr3_sharpa_lora_v1`（或 `fullft_v1`）先严格核对字段名称，选取位置并重排为“左臂、左手、右臂、右手”；三路相机保持 cam0 外部、cam2 左腕、cam1 右腕。原有 58D baseline 不变。

训练应指向一套固定、已封存的批次，重新计算与该批数据、适配和 horizon 绑定的统计。该训练适配属于独立训练工程，不是接收器依赖。实际来源、归一化和 JAX 数据输入验证摘要见 [集成验证记录](docs/integration_validation.json)；记录只证明数据链路已验证，不代表训练或实机效果已验证。

`--compare-source` 覆盖所选状态、动作、无损传感器和采样时间，并完整解码 RGB。可另外执行只读的 RGB 内容抽查，比较每集首/中/尾和裁剪接缝附近的源图像：

```bash
python scripts/check_rgb_source.py --dataset /path/to/dataset --output outputs/new_rgb_report.json
```

该抽查报告 MAE/PSNR，不新增质量门槛，也不将有损 RGB 宣称为逐像素无损。清洗与 30 Hz 对齐本来就会删除等待段、丢弃未选中的高频消息或重复低频图像；LeRobot 不是所有 ROS 话题的逐消息备份，原始 rosbag 的服务器备份保留；采集端daily模式在备份核验后释放本机副本。执行来源比较需要当时的清洗包仍可访问；自动接收器在删除中间包前完成比较并归档结果。

## 工作机 GUI（auto-convert-transfer-ui）

```bash
./scripts/start_gui.sh
```

本机浏览器打开 `http://127.0.0.1:8765`。可配置多任务、本机 A/B 输入、202 原包与完整 LeRobot 目录、28 normal/speedup 独立目录；支持 SSH 文件夹浏览、新建场景自动带 A/B、保存、开始、正常停止、断点续传与实时日志。沿用既有清洗和 24h 批次逻辑，保留 Sharpa 116/58、Wuji 108/54，不自动训练。

默认创建空测试输入，不自动启动任务。新 GUI 配置和队列独立放在 `outputs/gui/`，不接管旧队列或迁移旧结果。目录、用户和端口都可编辑。安装与旧服务接管、路径层级、正常停止行为和验收边界见 [GUI 使用文档](docs/gui.md)。
