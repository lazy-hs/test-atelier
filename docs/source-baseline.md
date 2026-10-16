# 来源工程基线（2026-10-15）

> 本文件仅用于来源代码审计和哈希追溯，不是运行或部署手册。实际使用请参阅 [使用说明](USAGE.md) 和根目录 [README](../README.md)。

合并时只读取以下两个来源工程的当前工作树，不修改它们。两者均有未提交改动；此清单用于后续对照，不表示其仓库 HEAD 与当前文件一致。复制代码后在Test Atelier 内增加鉴权、项目归属、存储隔离和安全限制。

| 来源文件 | SHA-256 |
| --- | --- |
| `ecxel_check/server/converter.py` | `8fc3855b0140f12dfd81cebb17fb9333fcb67b3370c7a65261e2f9d6da290fb0` |
| `ecxel_check/server/xmind_converter.py` | `4755e666d8b88f5749cd450ee7a1d9c532f4f3172cbb824fd5b214eb3c65abd1` |
| `ecxel_check/server/history.py` | `5936148d42d64ef04b3ce2fcc78dc3df8a12d69e6ea25e453f1edf684f9c8547` |
| `ecxel_check/server/config.py` | `437cb42893a1ef4e55809f822bcd80b37c194cb92d14b028c503014e1c607171` |
| `ecxel_check/server/app.py` | `071981ef326c7049f0f64e35ef7d4f73d9c6c2f8caf482a189302193d11d6488` |
| `ecxel_check/resources/templates/ugos_pro测试用例导入模板_20260901155913.xlsx` | `bb719840e8fe37ce206a4b9354e1b13f2d75b3331bc61cd5c8ad9f6d872993a9` |
| `ecxel_check/web/src/App.jsx` | `f86061ce54eeb247e47e2f6e92fa79b8baba8a76bce0e712f88a75420997e12b` |
| `ecxel_check/web/src/api.js` | `fc956115ba08ca50b74c7a95f5218ba81441a727204058a59ebda72931d6b138` |
| `qa-doc-skills/scripts/generate_xmind.py` | `e88dcc2f674deb1c1feb7facb2a98e833fc9321d939ecac35734603538082693` |
| `qa-doc-skills/scripts/generate_excel.py` | `465e900e45d6ff30534dfc03755f260059c7a2284207cf01f7e311d1ea87085e` |
| `qa-doc-skills/skills/requirement-analysis/SKILL.md` | `22819cf3872969b628b6ec5bc1e9fa100deb63470ad141b0325188829bb1826c` |
| `qa-doc-skills/skills/test-cases/SKILL.md` | `204e3fbdc19c8062cf5cd5dc753a662f787d352548339ddacb748c4d26068b32` |

运行时采用规则文档和 XMind 脚本的本地副本；技能仓中的历史产物、`.memory` 与针对特定产品的监控中心基线没有并入通用 Test Atelier。
