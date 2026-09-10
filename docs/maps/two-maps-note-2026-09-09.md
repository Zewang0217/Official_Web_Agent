# 两个 Map 的观察笔记(落盘,待合并后深挖)

> 来源:用户 2026-09-09 指明「我新发现的,关于两个 map(rag 部分和简历 workflow 部分)的问题」,指向
> [#137](https://github.com/Boyuan-IT-Club/Official_Web_Agent/issues/137) 与
> 其评论 [#137#issuecomment-5598733951](https://github.com/Boyuan-IT-Club/Official_Web_Agent/issues/137#issuecomment-5598733951)。
> 本文件只落已核实的观察与待办,不作结论;用户合并 PR #143 后会指向新的 issue/comment 再深挖。

## 两个 Map 的定义(已核实)

- **RAG map**:[#117 客服知识库(RAG):面板加文档 + 带引用答疑](https://github.com/Boyuan-IT-Club/Official_Web_Agent/issues/117)
  → 夜跑执行板 [#136](https://github.com/Boyuan-IT-Club/Official_Web_Agent/issues/136)(实现照 #134 切片)。
- **B 简历评估 map**:[#122 B模块简历评估重建](https://github.com/Boyuan-IT-Club/Official_Web_Agent/issues/122)
  → 夜跑执行板 [#137](https://github.com/Boyuan-IT-Club/Official_Web_Agent/issues/137)(实现照 #135 切片)。

两者都在仓库的 issue 体系里作为「wayfinder:map」存在,夜跑执行板 #136/#137 是其实现切片载体。

## 从 #137 及评论已核实的点(2026-09-09)

- #137 正文:B1-B7 关票,B8 数据面全闭环验收通过;状态横幅定格在 2026-09-09 深夜用户叫停捣鼓。
- 评论 5598733951(恢复指引):backend 容器因 flyway/MySQL 竞态退出 → 一条命令恢复;
  本地测试入口(管理端 3000/用户端 3001/Agent 8001,周期 3 四条卡 + 0 分队列);
  遗留票:①双仓深挖(extract_repo 只取第一仓)、②评审页周期选择器改下拉、
  ③评测错因线无真机数据、④知识库语料官方审定。
- 评论末尾明确:`PR #143 在 GitHub 上 OPEN 未合并`(API mergedAt=null)——需点合并。

## 用户给的执行顺序(必须遵守)

1. **先修刚说的 bug(ContextVar 端到端传播)→ 测试 → 用户去合并 PR #143。**
2. 合并完成后,用户指向「这个 map 的新的 issue/comment」再深挖两个 map 的问题;
   到那时再评估是否需要 grill,并落更完整的分析。

## 待办(合并后)

- [ ] 读用户指的新 issue/comment,定位「两个 map 问题」的具体所指(协调/复用/进度/承接?)
- [ ] 评估是否需要 grill(分歧/设计取舍 → grill;纯进展类 → 普通跟进)
- [ ] 落完整分析文件(本文件仅为占位观察,不构成结论)