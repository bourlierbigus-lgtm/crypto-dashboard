# 任务：将加密货币日报脚本做成 Web 仪表盘

## 现有资源
- `crypto_report.py` — 数据采集脚本，已能获取所有数据（BTC/ETH行情、MA均线、恐慌贪婪指数、ETF净流入、合约持仓、资金费率、AHR999、NUPL、MVRV）

## 要求

### 技术栈
- 后端: Python FastAPI（复用 crypto_report.py 的数据采集逻辑）
- 前端: 单页面，纯 HTML + Tailwind CSS + 少量 JS（不要用 React/Vue 等框架）
- 数据接口: 后端提供 `/api/report` JSON 接口，前端 fetch 渲染

### 页面设计
- 深色主题，加密货币风格
- 顶部显示 BTC/ETH 当前价格和24h涨跌
- 中间用卡片展示各项指标（MA均线、市场情绪、ETF、链上指标）
- 底部显示"系统判断"区域，如果触发极高胜率区间要醒目提示
- 移动端适配

### 数据更新
- 后端启动时采集一次数据并缓存
- 提供 `/api/refresh` 接口手动刷新
- 前端显示上次更新时间

### 部署
- 用 uvicorn 运行
- 监听 0.0.0.0:8080
- 生成 requirements.txt
- 生成 start.sh 启动脚本

### 注意
- NUPL 和 MVRV 需要 Playwright 浏览器提取，在服务器上可能不可用，做好 fallback（显示 N/A）
- 不要改动 crypto_report.py 的核心采集逻辑，而是 import 复用
- 保持代码简洁
