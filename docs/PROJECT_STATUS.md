# FindIt 项目当前状态

## 2026-05-17 进展 — 爬虫重做 + 数据完整 + 离线清洗

**单会话 sweep 爬虫（`crawler/runner.py` 的 `sweep_keyword`）跑通跑稳：**
- 搜索 → 开帖（`open_note`，goto 驱动）→ 抓帖子正文 + 评论 → 抓作者/评论者主页，全程一个登录会话。
- modal 真关闭（`dismiss_modal`，ESC）；`pointer-events` 继承坑修复；`go_back` 落到无 token 裸 URL 改为 token 重导航。
- 多城市：深圳/广州/北京/上海 × 找对象/找男友/找女友/脱单/交友（`scripts/_smoke_sweep_multi.py`）。
- 节奏：抖动 3–10s（随机性 > 时长）；Fix1/2 提速；10+ 轮测试 0 风控、0 nav-failed。

**数据完整性：**
- 帖子正文入库（`scrape_note_body` → `post.content` 标题+正文）。
- `posts.parent_note_id` — 评论 ↔ 父帖可直接 JOIN。
- `authors.homepage_url` — 每个作者的主页 URL。
- DB 迁移自愈幂等（`_migrate_in_place`）。

**离线清洗（DATA_SPEC §2/§3，`scripts/backfill_filter.py`）：**
- 按 §3（红娘 / 中介 agency 短语 / 代发）+ §2（入库门槛）给每个作者盖 `crawl_state`。
- Rule A 修复「不是红娘」误杀；「中介」用 agency 白名单避免误杀真人。
- §2 IP 门槛放宽为「IP 非空 或 正文提到城市」。
- 结果：`kept` 207 / `filtered_out` 60 / `pending_profile` 2014。

## ✅ 已完成功能

### 1. 数据采集（基于 MediaCrawler）
- ✅ MediaCrawler 搜索 + 评论抓取（无需登录）
- ✅ 数据导入脚本（`scripts/import_mediacrawler.py`）
- ✅ 当前数据：55 帖子 + 55 作者 + 35 条交友评论

### 2. AI 标签提取（新增）
- ✅ AI 标签提取器（`findit/ai/tag_extractor.py`）
- ✅ 支持提取个人信息、交友要求、意图评分
- ✅ 数据库架构更新（添加 `ai_tags` 列）
- ✅ 命令行工具（`scripts/extract_tags.py`）
- ✅ 使用文档（`docs/AI_TAG_EXTRACTION.md`）

### 3. 数据过滤系统
- ✅ 共享过滤器（`findit/ai/filter_rules.py`）
- ✅ 用户过滤器（按地理位置、年龄等）
- ✅ 检测中介/营销号/不活跃账号

## 🚧 待完成功能

### 1. 匹配服务优化
- [ ] 基于提取标签的智能匹配
- [ ] 多维度匹配算法
- [ ] 匹配结果排序和推荐

### 2. AI 破冰话术生成
- [ ] 基于用户画像和对方标签的个性化话术
- [ ] 多种话术风格选择
- [ ] A/B 测试和效果追踪

### 3. Telegram Bot 集成
- [ ] 用户注册和资料填写
- [ ] 每日匹配推送
- [ ] 话术发送和回复追踪
- [ ] 用户反馈收集

### 4. 爬虫优化（可选）
- [ ] 并发处理优化
- [ ] 错误重试机制
- [ ] 增量更新策略

## 📊 数据统计（2026-05-17）

- **总帖子数**：416
- **评论数**：1952
- **作者数**：2281（已爬主页 266）
- **匹配池（crawl_state=kept）**：207

## 🛠️ 技术栈

- **后端**：Python 3.x
- **数据库**：SQLite
- **AI**：Anthropic Claude API (Sonnet 4.6)
- **爬虫**：MediaCrawler (本地修改版)
- **Bot**：Telegram Bot API (待集成)

## 🚀 快速开始

### 1. 配置 API Key

```bash
cd ~/Desktop/findit
nano .env  # 添加 ANTHROPIC_API_KEY=sk-ant-xxxxx
```

### 2. 运行标签提取

```bash
# 处理前3个帖子
python scripts/extract_tags.py --limit 3

# 只处理评论
python scripts/extract_tags.py --limit 10 --source-type comment
```

### 3. 查看结果

```bash
sqlite3 data/findit.db "SELECT id, ai_tags FROM posts WHERE ai_tags IS NOT NULL LIMIT 5"
```

## 📁 关键文件

```
findit/
├── findit/
│   ├── ai/
│   │   ├── tag_extractor.py    # AI 标签提取器
│   │   ├── scorer.py            # AI 评分系统
│   │   └── filter_rules.py      # 规则过滤
│   ├── db/
│   │   └── database.py          # 数据库层
│   └── config.py                # 配置管理
├── scripts/
│   ├── import_mediacrawler.py   # 数据导入
│   ├── extract_tags.py          # 标签提取
│   └── migrate_add_tags.py      # 数据库迁移
├── data/
│   └── findit.db                # SQLite 数据库
└── docs/
    └── AI_TAG_EXTRACTION.md     # 使用文档
```

## 💡 核心优势

1. **不依赖登录**：使用 MediaCrawler 绕过登录限制
2. **AI 驱动**：Claude API 提供高质量的标签提取和匹配
3. **增量处理**：支持增量更新，避免重复处理
4. **可扩展性**：模块化设计，易于添加新功能

## 🎯 下一步优先级

1. **配置 API Key** → 测试标签提取
2. **批量处理现有数据** → 构建标签数据库
3. **实现匹配算法** → 基于标签的用户匹配
4. **集成 Telegram Bot** → 用户交互界面

## 📝 技术决策记录

### 选择 MediaCrawler 的原因
- ✅ 无需登录即可搜索和抓取评论
- ✅ 已经过验证，可以规避风控
- ❌ 用户主页笔记需要登录（暂时跳过）

### AI 标签提取策略
- ✅ 使用帖子/评论内容本身进行分析
- ✅ 不依赖用户主页笔记列表
- ✅ 多维度结构化输出（个人信息 + 要求 + 意图）

### 成本控制
- 💰 预估：每个帖子 $0.0002-0.0003
- 💰 1000 帖子：$0.20-0.30
- 💰 增量处理避免重复成本

---

项目状态更新时间：2026-04-10
分支：claude/xiaohongshu-matchmaker-mvp-tkvxq