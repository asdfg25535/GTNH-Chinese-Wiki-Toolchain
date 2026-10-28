# 任务书数据导出

在仓库根目录运行（仅生成本地数据，不访问 Wiki）：

```powershell
.\.venv\Scripts\python.exe questUpload/split_quests.py
```

脚本默认读取仓库根目录 `.env`，也可用 `--env-file PATH` 指定；同名环境变量优先。
需要 `MINECRAFT_DIR`、`MOD_REMOTE_NAME`、`MOD_VERSION`。相对 Minecraft 路径以
所选 `.env` 所在目录为基准，引号包围的 Windows 路径也可使用。

输入是 `MINECRAFT_DIR` 下的：

- `config/betterquesting/DefaultQuests/Quests/**/*.json`
- `config/betterquesting/DefaultQuests/QuestLines/*/QuestLine.json` 及同目录任务坐标文件
- `config/betterquesting/DefaultQuests/QuestLinesOrder.txt`
- `config/txloader/forceload/betterquesting/lang/zh_CN.lang`

输出沿用工具链版本目录：

```text
wikitext/Data/Versions/<MOD_REMOTE_NAME>/<MOD_VERSION>/
  QuestLines/0.json, 1.json, ...
  Quests/0.json, 1.json, ...
```

这些文件是 Data 命名空间的 JSON 数据页源码，例如
`Data:Versions/GTNH/2.9.0-RC2/Quests/0.json`，可由现有 `databaseDump/wiki_inventory.py`
和 `databaseDump/data_upload.py` 流程识别。脚本不生成展示模板或版本配置模块。

## 数据结构

任务线页面根字段为 `questLines` 数组，任务页面为 `quests` 数组。记录保留源字段，按以下规则整理：

- NBT 字段的 `:数字` 类型后缀移除，`:9` 列表按数字索引排序并转换为 JSON 数组。
- 所有 `…IDHigh:4` / `…IDLow:4` 对合并为 `…ID` 字符串：两个有符号 long
  以大端排列，URL-safe Base64 编码并去掉 `=`。任务 ID、任务线 ID、前置引用和布局引用使用相同格式，
  不会经过浮点数；其他 NBT long 也保存为十进制字符串，避免 Wiki/JavaScript 丢失精度。
- `properties.betterquesting.originalName` / `originalDesc` 保留源文件中的英文，
  `name` / `desc` 分别通过
  `betterquesting.quest.<ID>.name/desc` 或 `betterquesting.questline.<ID>.name/desc` 汉化。
  汉化字段中的 `%n` 转为真正换行；保留 `§` 格式码及 `[url]`、`[note]` 等任务书标签供展示端处理。
- 任务保留 `preRequisites`、`tasks`、`rewards` 和全部属性，物品及其 NBT 不裁剪。
- 任务线增加 `quests` 数组，包含 `questID`、`x`、`y`、`sizeX`、`sizeY` 等原布局字段；
  增加从 0 开始的 `order`，按 `QuestLinesOrder.txt` 顺序输出。
- 同一个任务可出现在多条任务线中，任务正文只输出一次。

缺少汉化时保留原文并打印缺失键；`--strict-translations` 可要求所有标题和描述均有译文。
源任务书中指向缺失任务的引用会保留并警告。重复 ID、无效 JSON、任务线顺序不一致等错误会阻止写入。

## 校验与重复生成

```powershell
.\.venv\Scripts\python.exe questUpload/split_quests.py --dry-run --strict-translations
.\.venv\Scripts\python.exe -m unittest discover -s questUpload -p "test_*.py"
```

输出采用两空格缩进、LF 换行和末尾换行的 JSON，保留中文原文；描述字符串内部的换行使用合法的 `\n` 转义。
每页默认最多 1,800,000 个 UTF-8 字节（包含缩进和换行），可用 `--max-bytes` 调小，最大为 2,000,000。
单条记录无法装入一页时会报错，不拆散记录。所有数据解析和分页通过后才开始写文件。
重新生成只更新这两个输出目录内的数字 JSON 页，移除已不需要的数字页；其他文件保留。
内容相同的页面不重写，以保留修改时间。`--dry-run` 完成同样的校验但不写文件。

## 上传任务数据

`upload_quests.py` 参照 `databaseDump/data_upload.py` 的机器人登录和并发编辑流程，
直接扫描当前版本的 `QuestLines`、`Quests` 数字 JSON 页，不需要先生成通用 Wiki 清单。

```powershell
# 预览待上传页面，不联网、不写状态库
.\.venv\Scripts\python.exe questUpload/upload_quests.py --dry-run

# 上传（默认 8 线程）
.\.venv\Scripts\python.exe questUpload/upload_quests.py

# 指定并发数、试传前 20 个待上传页面
.\.venv\Scripts\python.exe questUpload/upload_quests.py -t 2 --debug

# 忽略本地成功记录，重新上传所有任务页
.\.venv\Scripts\python.exe questUpload/upload_quests.py --force
```

上传同样读取仓库根目录 `.env`，支持 `--env-file PATH` 和同名环境变量覆盖。
`MOD_REMOTE_NAME`、`MOD_VERSION` 决定目标版本；`BASE_URL` 默认使用灰机 Wiki API；
`AUTHKEY` 沿用现有 API 认证；实际上传还需 `BOT_USERNAME`、`BOT_PASSWORD`。
上传已导出的文件不需要 `MINECRAFT_DIR`，预览不需要账号密码。

脚本仅编辑本次扫描得到的任务数据页，不处理物品、配方、版本模块或远端页面删除。
因此任务分页数减少后，远端多余旧页仍需另行清理。
开始联网前会校验两类数据页存在、分页编号连续、JSON 根字段及单页字节上限。

上传状态独立保存在 `questUpload/upload_state.db`（已加入 Git 忽略），
按 API 地址、页面标题、内容 SHA-256 判断是否需要上传。成功或 API 确认内容未变后才标为完成；
失败和中断页面保留待上传状态，重新运行同一命令即可续传。
上传使用扫描时的内容快照；运行期间本地文件发生修改，会在下一次扫描中识别。
状态库不用于查询远端当前内容；远端页面被他人修改或删除时可用 `--force` 重新同步。

CSRF token 失效时自动刷新重试一次，其他上传错误通过重新运行重试。
退出码：成功 `0`，校验或上传失败 `1`，Ctrl+C 中断 `130`。
