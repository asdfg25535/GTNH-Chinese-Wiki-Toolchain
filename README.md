# GTNH Chinese Wiki Toolchain

这是 [GTNH 中文维基](https://gtnh.huijiwiki.com/) 的本地工具链仓库。主要用于数据库导出后的上传，也用于维基日常编辑。如果用于日常编辑，通常仅仅需要查看 `AGENTS.md` 与 `wiki.py` 的内容。

如果运行了一次大规模的 版本更新/数据库功能更新/重构 任务，切实影响到了维基的`Data:` 页面，需要同步维护线上的 [Project:Database](https://gtnh.huijiwiki.com/wiki/Project:Database) 页面 作出必要的说明。

## 目录结构

- `wiki.py`：MediaWiki API 辅助脚本，负责登录、读写页面、文件同步等基础操作。
- `databaseDump/`：数据拆分、任务清单、图标与重定向上传的主流程。
- `questUpload/`：将 BetterQuesting 任务书汉化，导出并上传当前版本的 QuestLines / Quests JSON 数据页，见 [运行说明](questUpload/README.md)。

## 主要用途

这套工具主要做三件事：

1. 从 `dumps/` 里的原始导出数据拆分出适合维基上传的页面数据。
2. 扫描本地 `wikitext/` 与维基现有内容，生成上传/删除任务。
3. 批量上传页面、图标文件和图片重定向。

## 数据文件

仓库保存工具脚本和必要的任务数据库；原始 `dumps/` 数据存放在本地。

- `databaseDump/image_inventory.db` 会保留在仓库中，用于记录图片上传与重定向状态。
- `databaseDump/wiki_inventory.db` 每次都可以重新生成，不提交到仓库。

## 配置

复制 `.env.example` 为 `.env`，然后按本机情况填写路径和账号：

```bash
copy .env.example .env
```

需要重点修改：

- `DUMPS_DIR`：Minecraft 导出的 `dumps` 目录。当前数据源由 [iouter/GTNH-Dumper](https://github.com/iouter/GTNH-Dumper) 生成，`DUMPS_DIR` 应指向这个工具导出的目录。
- `BASE_URL`：目标 wiki 的 MediaWiki API 地址，默认是 `https://gtnh.huijiwiki.com/api.php`。
- `AUTHKEY`：wiki API 需要的认证 key。
- `EDITOR_USERNAME` / `EDITOR_PASSWORD`：普通编辑账号，供 `wiki.py` 使用。
- `BOT_USERNAME` / `BOT_PASSWORD`：批量上传账号，供 `databaseDump` 里的上传脚本使用。

`.env` 不会提交到仓库。

## 运行方式

通常先运行拆分和任务生成，再执行上传脚本：

```bash
python databaseDump/split_all.py
python databaseDump/wiki_inventory.py
python databaseDump/image_inventory.py
python databaseDump/data_upload.py
python databaseDump/image_upload.py
python databaseDump/image_redirect.py
```

`MOD_LOCAL_NAME` 选择本地导出来源，`MOD_REMOTE_NAME` 与 `MOD_VERSION` 决定 Wiki 页面名称。
例如本地模组名为 `sciencenotleisure`、远端名为 `GTNotLeisure`、版本为 `0.2.7-rc2` 时：

- 数据页位于 `Data:Versions/GTNotLeisure/0.2.7-rc2/`。
- 版本配置模块为 `Module:Database/Versions/GTNotLeisure/0.2.7-rc2`，Wiki 标题不带 `.lua` 后缀。
- 本地模块文件为 `wikitext/Module/Database/Versions/GTNotLeisure/0.2.7-rc2.lua`。

拆分物品、矿物词典或配方后，`split_all.py` 会按最终数据生成这个模块，返回的通用版本配置中，
`storage` 保存 Items、ItemNames 和配方的实际桶数。`wiki_inventory.py` 将它与当前版本的数据页
一起纳入任务清单，沿用文件修改时间、大小与上传状态判断是否需要更新。

`data_upload.py` 先上传数据页，全部成功后再上传版本配置。失败或中断时保留待上传任务，
重跑即可继续；`--debug` 只试传数据页，版本配置留待完整上传。旧的预处理目录需用当前脚本
重新生成模块并重建任务清单。

首次启用新版本时，在 `Module:Database/Versions` 的 `versionModules` 中登记上述模块路径，
再按发布安排更新版本别名；同一路径的旧数据换成新格式后，从 `legacyVersions` 移除对应版本。
这些公共版本入口由维护者管理，自动上传只更新当前版本的配置模块。
