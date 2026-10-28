# databaseDump

这是 GTNH 中文维基工具链中的数据导出、整理与上传子目录。

## 工作原理

整体流程分三段：

1. 先从 Minecraft `dumps/` 里拆出适合 wiki 上传的页面数据。
2. 再扫描本地 `wikitext/` 和 wiki 现有内容，生成上传清单数据库。
3. 最后按清单批量上传页面、图标和图片重定向。

## 主要脚本

- `split_all.py`：读取原始导出数据，合并图标/流体等信息，并切分成 wiki 可上传的小文件。
- `item_names.py generate-config`：生成本地中文图片候选报告，供编辑首选配置时参考。
- `wiki_inventory.py`：扫描本地 `wikitext/`，对比 wiki 现有页面，生成 `wiki_inventory.db`。
- `image_inventory.py`：扫描 `dumps/icons/` 和重定向映射，生成 `image_inventory.db`。
- `data_upload.py`：读取 `wiki_inventory.db`，把待上传页面推到 wiki。
- `image_upload.py`：读取 `image_inventory.db`，上传图标文件。
- `image_redirect.py`：读取 `image_inventory.db`，创建 `File:` 图片重定向页。

## 数据库

- `wiki_inventory.db`：页面上传任务表，每次运行 `wiki_inventory.py` 都可以重新生成，不提交到仓库。
- `image_inventory.db`：GTNH 图标上传与重定向任务表，会提交到仓库用于保留图片上传状态。
  其他模组的图片状态独立保存在 `source_state/<本地模组名>/<MOD_VERSION>/image_inventory.db`，不提交到仓库。

## 配置

脚本默认从仓库根目录的 `.env` 读取配置。先复制 `.env.example` 为 `.env`，再按本机环境填写：

```bash
copy .env.example .env
```

其中 `DUMPS_DIR` 对应 Minecraft 导出的 `dumps` 目录。当前数据源由 [iouter/GTNH-Dumper](https://github.com/iouter/GTNH-Dumper) 导出，填写时应让 `DUMPS_DIR` 指向 GTNH-Dumper 生成的 dumps 目录。

## 常用命令

在 `.env` 中设置上传目标，例如：

```dotenv
MOD_LOCAL_NAME=GTNH
MOD_REMOTE_NAME=GTNH
MOD_VERSION=2.9.0-beta-1
```

可用 `MOD_LOCAL_NAME` 和 `MOD_REMOTE_NAME` 分别指定本地数据源名和远端目录名。
两项均须填写，缺失或为空时会报错。例如：

```dotenv
MOD_LOCAL_NAME=TwistSpaceTechnology
MOD_REMOTE_NAME=TST
MOD_VERSION=1.0.0
```

此时从 `dumps/filtered/TwistSpaceTechnology/` 读取，上传到 `Data:Versions/TST/1.0.0/`。
`split_all.py` 输出到 `wikitext/Data/Versions/<远端模组名>/<MOD_VERSION>/`；
`wiki_inventory.py` 只扫描和查询该模组/版本，`data_upload.py` 只执行该前缀下的上传和删除任务。
本地模组名为 `GTNH` 时读取 dumps 顶层；其他模组读取
`dumps/filtered/<本地模组名>/`。远端名用于生成上传页面的目录。
模组目录不存在会报错；缺失或空的主输入文件会跳过。NBT 扫描缓存按本地模组名/版本保存在
`databaseDump/source_state/` 中。页面清单支持 filtered 目录中的文件。
修改配置后应重新运行拆分和清单脚本。缺少配置时，按报错提示补齐 `.env` 后重试。
普通 Wiki 页面源码上传仍使用文件对应的页面路径。

```bash
python split_all.py
python wiki_inventory.py
python image_inventory.py
python data_upload.py
python image_upload.py
python image_redirect.py
```

## 矿辞与物品依赖

`split_all.py oredict` 使用当前模组、版本已生成的 `Items` 分页（JSON 或 TABX），
将 `:32767` 通配成员展开为实际导出的 damage/NBT 物品 key。首次处理该版本时运行：

```bash
python split_all.py items oredict
```

Items 目录缺失、没有分页或分页中没有物品记录时，矿辞处理会报错，保留已有矿辞页和
`oredictionary-associations.json` 报告。Items 生成成功后才可单独运行 `python split_all.py oredict`；
物品数据变化后应先重新生成 Items。默认的 `python split_all.py` 已按 Items、矿辞的顺序执行。

## 神秘时代研究表

默认拆分任务包含研究表，也可单独运行 `python split_all.py tc4research`。
它将当前数据源的 `tc4research.json` 写为
`Data:Versions/<远端模组名>/<版本>/Tc4research/0.json` 等数字分页，
保留 `tc4Researches` 数组及研究原字段。每页按最终 UTF-8 字节分页，严格限制在 2,000,000 字节以内。

每次运行处理 `.env` 中选择的一个模组与版本。`MOD_LOCAL_NAME=GTNH` 时读取完整研究表，
排除 [config/addon-mods.json](config/addon-mods.json) 列出的额外附属来源；
Thaumcraft、dreamcraft、KekzTech 等基础包模组和缺少来源字段的旧记录保留。
公共列表填写注册 Mod ID，目前包含 `TwistSpaceTechnology`、`sciencenotleisure` 和
`programmablehatches`，供数据范围划分复用，目前用于研究表。

处理附属时，`MOD_LOCAL_NAME` 填注册 Mod ID，`MOD_REMOTE_NAME`、`MOD_VERSION` 指定目标目录。
附属优先读取自身 filtered 目录里的研究表；缺少独立导出时，从完整研究表按
`sourceMod` 精确选择注册者，比较时忽略大小写。该字段由 Dumper 在研究注册时记录，研究可以位于任意分类。
尚无该附属的 filtered 目录时，使用 `python split_all.py tc4research` 单独处理研究；
默认全部任务及其他任务需要附属自己的数据目录。
完整旧导出尚无来源字段时跳过附属提取，已有独立 filtered 研究表继续使用。
新增额外附属时，将注册 ID 加入公共列表，再分别处理 GTNH 和该附属的目标版本。

## 配方输入与输出归类

`split_all.py recipes` 在关联物品 NBT 和切分页面前，使用 `recipe_roles.py` 按配方来源归类
NEI 的附加槽位。已确认的燃料、灌注台试剂和作物基因提取器的数据介质写入 `inputItems`，
副产物和掉落写入 `outputItems`；巫师烤炉分别归类燃料、黏土罐与副产物。
归类保留候选顺序、NBT、概率和不消耗标记，缺少燃料的烤炉保留对应输入空位。

研究站中已写入结果的数据棒、魔力池催化条件及其他展示集合继续保存在 `otherItems`，
由对应界面解释和显示。已归类的数据可直接用于输入、输出查询。

## 图片上传

`image_inventory.py`、`image_upload.py`、`image_redirect.py` 同样读取 `.env` 的模组配置：

- 本地模组名为 GTNH 时使用 `dumps/icons/`，其他模组使用 `dumps/filtered/<本地模组名>/icons/`。
- 中文图片重定向使用预处理生成的 `source_state/<本地模组名>/<版本>/image_redirect.resolved.csv`。
  原始 `image_redirect.csv` 保留作参考。重名选择规则见下文。

依次运行图片清单、图片上传、图片重定向三个脚本。非 GTNH 模组首次运行会创建独立数据库，
GTNH 保留已有数据库和上传记录。图片任务只选择当前来源中存在的文件，重定向任务只选择当前 CSV
计划中仍有效且目标图片已上传的记录。失败的图片保持待更新状态，重复运行可以重试。

三个图片脚本都会在执行前通过 `image_plan.py` 重新计算当前 PNG 的哈希，需要安装 Pillow：
`python -m pip install Pillow`。运行 `image_inventory.py` 生成本地计划和状态，随后由上传脚本执行。
计划保存在同一状态目录的 `image-upload-plan.json`，包含实体上传列表、最终重定向和各组哈希。

只在同一注册物品 `modid:name` 内比较不同 damage/NBT 变体。哈希覆盖尺寸与完整 RGBA
像素，PNG 压缩和文字元数据不同不影响比较；动画条带比较全部帧，APNG 比较逐帧合成后的完整 RGBA 画布、帧时长、循环次数和默认图像。
同图优先选无 NBT、damage 为 0 的图片；缺少该项时依次按无 NBT、零 damage、damage 数值、
完整 shortKey 和文件名排序。每个注册物品分别选择代表图片。

同图变体保留自己的文件名，通过重定向指向代表图片并退出上传列表；中文名先按人工配置
确定物品，再直接指向该物品最终代表图片。因此中文名和变体别名都是一级重定向。
物品 key、NBT、配方数据、本地 PNG 和已上传文件历史均保留。额外素材沿用原有上传方式。

Wiki 图片标题为 `File:<图标文件名>`；模组和版本用于选择数据源及本地清单。

### 中文重名图片的人工选择

物品预处理会自动扫描重名，生成诊断报告和重定向表。人工选择和默认规则确定的标题进入
重定向任务，其余标题留在候选报告中，供编辑者选择。
名称经过文件名清理、空格/下划线归一和首字母规范化之后发生的冲突也会被合并检查。

在本目录运行：

```bash
# 先生成最终物品 key、名称索引和重名报告。
python split_all.py items

# 生成候选报告，查看各名称对应的物品。
python item_names.py generate-config --output image-name-candidates.json

# 编辑 config/preferred-items.json 的明确约定后，重新应用选择。
python split_all.py items
```

默认读取 `config/preferred-items.json`（`schemaVersion: 2`）。每个标题的 `key` 是明确指定的
完整物品 key，优先于自动规则；显式 `null` 表示跳过该别名。未配置的名称自动使用默认偏好：
先优先选择 tooltip 中没有“弃用”“移除”或 `deprecated` 标记的物品，再按
Minecraft → 流体身份 `fluid.*` → GregTech → IC2 → 其他模组选择。在同一物品身份内优先无 NBT
版本，在同一注册物品内优先 damage 为 0 的版本。GT 矿石优先普通石头底岩；天然状态和
小型矿石属性仍保留区分。筛选后仍不能唯一确定的名称留在本地未解决报告中。
同一 rawKey 没有无 NBT 版本时，按完整 key 字典序选择第一个作为图片代表，配方继续使用各自的完整 key。
GT 电池箱按 tooltip 选择 1 槽版本。多个流体优先选择 `registeredBy=gregtech` 的候选；
此字段来自 Forge 注册表；该规则适用于包含此字段的数据。
同模组不同注册物品、没有零 damage 且无专门规则的多个子类型仍留给人工。
矿石规则适用于 GT `gt.blockores2`～`gt.blockores7`。

需要先为剩余冲突生成一版可编辑的默认清单时，使用同次导出的 `itemicon.json`：

```bash
python item_names.py generate-preferred --items-dir <预处理的Items目录> \
  --icon-map <导出目录>/itemicon.json --output preferred-items.generated.json
```

该命令保留已有人工选择和显式 `null`，仅补充现有规则筛选后仍有多个候选的名称，新增条目只保存 `key`。
在剩余候选中，选择 `itemicon.json` 中最先出现的图标；同图标的多个身份按完整 key 排序。
这与 Dumper 为中文重名依次分配无编号、` 2`、` 3` 别名时使用的遍历顺序一致，
不代表物品优劣，且可能随模组版本、注册顺序和配方变体变化。生成结果固定保存完整 key。
规则已唯一确定的名称继续使用自动规则；缺少候选图标映射时停止生成，需核对数据来源。
输出是独立的 `schemaVersion: 2` 清单，不覆盖公共首选文件，也不会自动应用。
可检查生成清单后通过 `IMAGE_NAME_CONFIG` 指定它，再重新预处理。

`null` 表示不发布该中文图片重定向。下面使用演示数据：

```json
{
  "schemaVersion": 2,
  "choices": {
    "示例物品.png": {"key": "example:item:3", "reason": "编辑者确认的显示约定"},
    "不发布的别名.png": {"key": null, "reason": "不为该名称发布图片别名"}
  }
}
```

公共清单保存已采用的选择，通过 PR 修改 `key`；`reason` 是可选的人工说明。
当前清单包含 2.9.0-RC-2 本体与附属模组快照中按 Dumper 顺序补齐的默认选择，自动生成的条目只保存 `key`。
本体与附属共用这份清单。例如“引力透镜”明确选择 GT 物品；独立处理 TST 时，
所选 GT key 不在候选中，该中文别名会记录到失效选择报告并跳过，TST 图标仍按完整物品身份处理。
`IMAGE_NAME_CONFIG` 可显式指定同格式的自定义清单，替代默认公共清单。
候选列表写入本地 `image-name-candidates.json`，冲突和失效选择写入
`image-name-resolution.json`。这两个文件供本地检查，公共 PR 修改 `config/preferred-items.json`。
失效 key 或已消失名称会报告并跳过，请在报告中核对并更新首选配置。
配置、原物品数据或图标映射变更后必须重新预处理；图片发布前会验证当前配置路径和文件的 SHA-256，切换配置路径后也须重新预处理。

图片状态使用 `images` 与 `redirects` 两张本地表，支持多个中文别名指向同一个 PNG。
首次运行图片清单时会迁移旧表中的上传状态。图片清单及上传脚本负责维护该数据库。
旧表中同一个中文标题关联了多张图片时，该标题重新标为待更新，按当前人工选择纠正目标。

### 中文名称查询索引

`split_all.py` 处理 `items`、`oredict` 或 `recipes` 后，依据最终物品、矿辞和配方刷新
`ItemNames/<桶号>/<分块号>.json`。名称按本模组、本版本的索引字节量选择桶数，页内保留实际 `bucketCount`。
以下为结构示例：

```json
{
  "bucketCount": 64,
  "names": {
    "示例容器": {
      "keys": ["example:container", "example:container_nbt"],
      "rawKeys": ["example:container"],
      "sourceHandlers": ["example/FillingHandler"],
      "usageHandlers": [
        "example/EmptyingHandler",
        ["minecraft/ShapedRecipeHandler_crafting", [0, 3, 12]]
      ]
    }
  }
}
```

`names[中文名]` 收纳该名称的全部查询信息：

- `keys`：最终完整 key 数组，保留同名候选和 NBT 变体。
- `rawKeys`：去重的基础身份数组，包含注册名和 damage，用于配方输出分桶及单产物查询的变体合并。与 `keys` 相同时省略。
- `queryKeys`：可选的 `{"完整key":"语义queryKey"}` 映射，例如蜜蜂物种身份。
- `sourceHandlers`：能获得该名称物品的处理器列表。
- `usageHandlers`：用途条目列表；字符串表示整个处理器，`[handler, [编号...]]` 表示该输入名称涉及的桶号或普通文件号。

handler 使用 `Recipes` 下的完整 `mod/handler` 路径。同名候选的路径取并集，包含矿辞和语义身份对应的配方。
来源列表同时收集基础身份的产物，供单产物名称查询合并 NBT 变体。
`otherItems` 同时计入来源和用途；专用掉落集合计入来源，`keyItem` 计入用途。空列表省略。
图片的首选候选由图片配置单独决定。

输入编号在配方最终拆分后生成，复用同一种二元数组结构。版本配置 `storage.recipeBuckets` 中的处理器将编号解释为桶号，其余处理器解释为普通文件号。
分桶索引启用有序合成、匠魂弩箭固化、组装机、合金炉回收和锻造锤；桶号 0 对应目录 `00/`。
普通文件索引启用伪装板合成、基因采样机、电弧炉回收、组装机、流体提取机回收、研磨机、研磨机回收、微光嬗变和包含要素的物品；文件号 0 对应 `0.json`。
普通分页中，单文件处理器以及覆盖全部文件的名称保存字符串条目，其余保存所涉及的文件号。
同名物品、矿辞和语义身份的编号取并集，多输入查询取交集；分桶时继续与输出定位的桶集合取交集。
锻造锤的 `shared/` 页随数字桶一起读取；`[handler, []]` 表示该名称仅涉及 shared 配方。
桶内全部分页参与查询；普通文件号交集为空时返回空结果。名称索引、最终配方文件和版本配置同步更新。

同一名称可以跨页，各页数组和 `queryKeys` 映射合并后再使用；若合并后的记录没有 `rawKeys`，则采用 `keys`。
默认每页最多 1,800,000 字节，最终检查计入所有字段和标点的 UTF-8 字节数，硬上限为 2,000,000 字节。
Lua 查询一次读取命中名称桶的所有分页，使用 `$objectToArray` 按名称字面值筛选，支持名称中的点号和 `$`。
精确名称转换同时取得 handler；多个输入的用途列表与多个输出的来源列表按版本取交集，填写 `type` 时再与指定处理器范围相交。
另查 Fluids 得到的候选、模糊名称和 `@key` 使用各自的查询条件。各版本使用覆盖身份和矿辞候选并集的名称参与 handler 交集。

### 物品 key 分桶

`split_all.py items` 生成 `Items/<桶号>/<分块号>.json`。它以最终 `key` 分桶，
保存每个物品的完整记录，包括名称、说明、图片映射、NBT 和原 `index`。
原 `Items/` 根目录数字顺序分页在生成成功后清理，只保留一份物品数据。查询端取得 key 后计算对应桶，再在桶内精确匹配 key；
同一桶超过大小限制时需要读取该桶的所有分块。

### 可选配方分桶

```bash
# 先完成物品处理；配方内部先做 NBT 关联，再生成可选分桶。
python split_all.py items recipes --recipe-buckets
```

启用后，适配池原位写到 `Recipes/<原机器目录>/<桶号>/<分块号>.json`，成功生成清单后删除对应机器根目录的旧数字顺序页。
未启用分桶或不在适配范围的配方池仍使用 `Recipes/<原机器目录>/<分块号>.json`。
范围固定为有序合成、无序合成、匠魂弩箭固化、匠魂部件压模、组装机、合金炉回收、流体固化器、锻造锤
这八个主要 handler，并且只在当前来源已拆成多个文件时处理。其他 handler 保留原文件。
输入为关联完成后的产物 `rawKey`，缺省时使用产物 `key`；同一注册名和 damage 的 NBT 变体共用一个逻辑桶。
完整 `key`、NBT 和 `queryKey` 原样保存在配方中，供精确匹配与显示使用。
单输出槽允许物品或流体；嵌套候选须归约为同一个路由身份。
锻造锤的多输出配方单独写入该 handler 的 `shared/<页>.json`；查询锻造锤时同时读取目标桶与 shared 目录的全部数字分块。
其余七个 handler 若出现多输出、`otherItems` 或无可识别输出，整池保留原文件，诊断写入 manifest。
每条配方写入一个桶。同槽候选的完整 key 可以不同，只要其 `rawKey` 相同即可归同桶；
存在多个路由身份或没有可识别身份时，整池保留顺序分页。
记录保留原 `index`，配方身份由 `(handler, index)` 确定。
`Recipes/routing-manifest.json` 的 `writtenRecipes` 记录实际写入条数，并列出处理的 handler、原文件、分桶文件和共享文件，便于读取端定位。读取配方时遍历清单指向的数据文件。
各 handler 按当前模组、当前版本的实际数据量选择桶数。小附属的组装机与 GTNH 的组装机可以采用不同桶数；少于两个来源文件时保留顺序分页。

每个配方页的 `routing.bucketCount` 和清单的 `handlers[handler].bucketCount` 标明该池桶数。
`routing.identityFields` 为 `["rawKey", "key"]`，表示按此顺序取首个非空字段作为路由身份。
清单的 `recipeBuckets` 为 `{handler: 桶数}`，只包含本次实际生成的池；读取端用此表同步路由配置。

重新生成分桶时，从原始 dumps 运行 `split_all.py`。分桶器接受顺序分页的配方文件。

查询端读取目标桶后，按原有条件匹配、排序和分页；未指定产物时需遍历该配方池的全部数据文件。
当前 Lua 先按数据页路径取候选，再对候选排序。重新分桶会改变文件路径，因此默认首条和分页顺序可能变化；完整匹配结果保留。

处理物品、矿辞或配方后，`source_state/<本地模组>/<版本>/version-config.lua` 从最终页面读取实际桶数，生成可发布到 `模块:Database/Versions/<远端模组>/<版本>` 的配置。数据页和此版本配置一同更新，并在 `模块:Database/Versions` 的 `versionModules` 中登记该模块。

### 分块哈希约定

名称索引以中文名为输入，物品分桶以完整 `key` 为输入，配方分桶以产物 `rawKey`（缺省时为 `key`）为输入。
三者共用 `routing_hash.bucket_id(text, bucket_count)`，调用时传入当前数据集或 handler 的实际桶数。JSON/TABX 页的 `bucketCount` 标明实际桶数，哈希算法不变：

1. 对输入的 UTF-8 字节计算 SHA-256。
2. 前后各 16 字节逐字节异或。
3. 按 Dumper 的 `0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ` 字符表，保留低 15 位 Base62，左侧补零。
4. 将该 15 位值对当前配置的桶数取模。桶号至少两位，例如 `00`、`63`、`100`、`127`。

前三步与当前 Dumper 的 NBT 后缀算法一致，测试包含现有 `{}` → `gLuB0gegBqh1aRe` 向量。
对于 16、32、64 或 128 桶，桶号也等于
`(digest[15] XOR digest[31]) % bucketCount`（字节从 0 计数），因此无需在 Lua 中用浮点数表示完整哈希整数。

## 独立 mod 版本数据

在 `.env` 中分别配置本地目录名、远端目录名和版本号，例如上文的
`MOD_LOCAL_NAME=TwistSpaceTechnology`、`MOD_REMOTE_NAME=TST`、`MOD_VERSION=1.0.0`。
然后在仓库的 `databaseDump` 目录依次运行：

```bash
python split_all.py
python wiki_inventory.py
python data_upload.py
```

来源为 `dumps/filtered/TwistSpaceTechnology/`，配方目标例如
`Data:Versions/TST/1.0.0/Recipes/gregtech/GTNEIDefaultHandler_assembler/0.json`。
修改配置后重新运行拆分和清单。页面清单和上传任务范围使用远端名及版本号。

仅处理原拆分器支持的数据类型。缺失或零字节的输入会提示跳过，CSV 清单和筛选报告
不生成数据页。物品所需的 `itemicon.json`、`fluid.json` 优先取本地模组目录，缺失时
从完整 dumps 读取作为关联数据。

### 分桶文件大小限制

`Items`、`ItemNames` 和每个已适配配方池分别统计紧凑 JSON 的 UTF-8 数据字节数，按目标大小向上取整，再取不小于该值的最小 2 的幂作为桶数：1、2、4、8……。例如目标为 1,800,000 字节时，10 万字节用 1 桶，500 万字节用 4 桶。TABX 按最终行数据统计；配方的 shared 记录独立分页。
桶数控制平均数据量；哈希分布不均、页头和单条记录大小由桶内分页处理。每个桶的所有数字分页都参与查询。
默认分页上限为 **1,800,000 字节**；可调参数 `max_bytes` 必须为正整数且不得超过硬上限
**2,000,000 字节**（十进制 2 MB）。所有页面按最终紧凑 JSON 的 UTF-8 字节数分页，
计入中文、emoji、metadata、括号和逗号；写入前再次检查完整字节长度。
选择桶数时累计各条记录的编码长度；分页时逐条编码和累计，避免反复序列化整批。

名称索引的 `names[名称]` 长数组可跨文件分页，读取端应合并各页同名数组。
单条物品、配方、名称或 key 超出一页容量时，生成停止并报告错误。
`Recipes/routing-manifest.json` 同样受 2,000,000 字节硬上限约束，超限时生成失败。
生成失败后应修正数据或分页参数并重新生成成功，再执行清单和上传步骤。
