# 许二木海龟汤合集 - OCR 构建

PDF 转 Markdown，使用 PaddleOCR（AI 中文 OCR）+ 连贯性标注。
GitHub Actions 自动构建。

## 使用

手动触发 Actions 即可自动执行：
1. 渲染 PDF 为 PNG（300 DPI）
2. PaddleOCR 全文识别
3. 构建 `许二木海龟汤合集.md`（140 道汤，按赛季分节）
4. 生成 `OCR_不连贯标注.md`（标记可疑的识别错误供手动矫正）
5. 自动提交结果到仓库
