-- ─────────────────────────────────────────────────────────────────────────
-- B 模块(简历评估)本地种子——依赖并扩展 local-test-data.sql,仅限本地 dev!
--
-- ⚠️ 安全闩:本文件必须与确认变量同会话执行,否则第一条语句即中止:
--   (echo "SET @local_seed_confirm='YES-I-AM-LOCAL-DEV';"; \
--     cat deploy/seed/local-dev-b-seed.sql) | \
--   docker exec -i <mysql容器> mysql -uroot -proot --default-character-set=utf8mb4 official
-- 生产库绝不应执行:脚本会在未确认时 SIGNAL 45000 中止,且字段/简历
-- 全部落在 9500+ 显式 ID 段(与生产自增段无交集)。
--
-- 幂等:全部 DELETE(9500 段)+ 重插,可重复执行。
-- 内容:为 local-test-data.sql 的四位候选人(9002-9005)补 B 模块评分面
--   字段(个人简介/加入理由/技术栈/项目经验/获奖经历,9501-9505)与四种
--   典型简历内容:9501 陈小明=带真仓(深挖线)/9502 林雪=敷衍(绝对卡 0 分
--   队列)/9503 张远=中等无仓(引导题)/9504 刘淇=获奖(DDG 现查线)。
-- ─────────────────────────────────────────────────────────────────────────

-- ── 安全闩(未确认即中止;CALL 抛 45000 → 管道模式 mysql 直接退出)────────
DELIMITER ;;
DROP PROCEDURE IF EXISTS local_seed_guard ;;
CREATE PROCEDURE local_seed_guard()
BEGIN
    IF IFNULL(@local_seed_confirm,'') <> 'YES-I-AM-LOCAL-DEV' THEN
        SIGNAL SQLSTATE '45000'
            SET MESSAGE_TEXT = 'ABORT: 这是本地开发种子数据。执行方式见文件头(需先 SET @local_seed_confirm)';
    END IF;
END ;;
DELIMITER ;
CALL local_seed_guard();
DROP PROCEDURE local_seed_guard;

-- ── 周期 3 兜底(fresh 库可能没有;已有则不动)────────────────────────────
INSERT INTO recruitment_cycle (cycle_id, cycle_name, description, start_date, end_date, academic_year, status, is_active)
SELECT 3, '2026 秋季招新', 'B 模块本地种子周期', '2026-09-01', '2026-10-31', '2026-2027', 1, 1
WHERE NOT EXISTS (SELECT 1 FROM recruitment_cycle WHERE cycle_id = 3);

-- ── 依赖对齐(与 local-test-data.sql 同 ID;已执行过该文件则此处无损)──────
INSERT INTO user (user_id, username, password, name, email, phone, major, status, is_deleted) VALUES
  (9001, '10245101666', '$2a$10$Es9rKAAwSn8vKUMD/EnVUOyDAqb5bFeh6BrD1vzOFeXC2nAOubhFi', '王调试', '10245101666@stu.ecnu.edu.cn', '13800001666', '计算机科学', 1, 0),
  (9002, 'test_chenxm',    '$2a$10$Es9rKAAwSn8vKUMD/EnVUOyDAqb5bFeh6BrD1vzOFeXC2nAOubhFi', '陈小明', 'chenxiaoming@stu.ecnu.edu.cn', '13812340001', '软件工程', 1, 0),
  (9003, 'test_linxue',    '$2a$10$Es9rKAAwSn8vKUMD/EnVUOyDAqb5bFeh6BrD1vzOFeXC2nAOubhFi', '林雪',   'linxue@stu.ecnu.edu.cn',       '13812340002', '数据科学', 1, 0),
  (9004, 'test_zhangyuan', '$2a$10$Es9rKAAwSn8vKUMD/EnVUOyDAqb5bFeh6BrD1vzOFeXC2nAOubhFi', '张远',   'zhangyuan@stu.ecnu.edu.cn',    '13812340003', '人工智能', 1, 0),
  (9005, 'test_liuqi',     '$2a$10$Es9rKAAwSn8vKUMD/EnVUOyDAqb5bFeh6BrD1vzOFeXC2nAOubhFi', '刘淇',   'liuqi@stu.ecnu.edu.cn',        '13812340004', '统计学',   1, 0)
ON DUPLICATE KEY UPDATE username = VALUES(username);
-- 角色绑定按 username 动态解析(10245101666 在库中可能是任意自增 id)
INSERT INTO user_role (user_id, role_id)
SELECT u.user_id, 1 FROM user u WHERE u.username = '10245101666'
ON DUPLICATE KEY UPDATE role_id = VALUES(role_id);
INSERT INTO user_role (user_id, role_id)
SELECT u.user_id, r.role_id FROM user u
JOIN (SELECT 4 AS role_id, 'test_chenxm' AS u UNION ALL SELECT 4, 'test_linxue'
      UNION ALL SELECT 4, 'test_zhangyuan' UNION ALL SELECT 4, 'test_liuqi') r
  ON r.u = u.username
ON DUPLICATE KEY UPDATE role_id = VALUES(role_id);
INSERT INTO resume (resume_id, user_id, cycle_id, status, resume_score, submitted_at) VALUES
  (9001, 9002, 3, 2, 0,  NOW()),
  (9002, 9003, 3, 2, 88, NOW()),
  (9003, 9004, 3, 4, 92, NOW()),
  (9004, 9005, 3, 3, 65, NOW())
ON DUPLICATE KEY UPDATE cycle_id = VALUES(cycle_id);

-- ── 评分面字段(5 textarea,9501-9505,cycle 3)────────────────────────────
DELETE FROM resume_field_value    WHERE value_id >= 95000;
DELETE FROM resume_field_definition WHERE field_id >= 9501 AND field_id <= 9505;

INSERT INTO resume_field_definition (field_id, cycle_id, field_key, field_label, field_type, placeholder, is_required, sort_order) VALUES
  (9501, 3, 'profile',       '个人简介', 'textarea', '请填写个人简介',       1, 10),
  (9502, 3, 'join_reason',   '加入理由', 'textarea', '请填写加入理由',       1, 11),
  (9503, 3, 'tech_stack',    '技术栈',   'textarea', '请填写技术栈',         1, 12),
  (9504, 3, 'projects',      '项目经验', 'textarea', '请填写项目经验',       1, 13),
  (9505, 3, 'awards',        '获奖经历', 'textarea', '请填写获奖经历',       0, 14);

-- ── 四种典型简历的评分面内容(value_id 95001+,resume 9001-9004)──────────
DELETE FROM resume_field_value WHERE resume_id IN (9001,9002,9003,9004)
  AND field_id IN (9501,9502,9503,9504,9505);

-- 9501 陈小明:项目带真仓 → 深挖线(deep_dive 四锚题)
INSERT INTO resume_field_value (value_id, resume_id, field_id, field_value) VALUES
  (95001, 9001, 9501, '我叫陈小明,软件工程 2023 级。大一起接触 Linux 和 Python,大二转向后端与安全方向,平时喜欢读源码和打 CTF。性格偏慢热但做事有始有终,带过三人的学习小组。'),
  (95002, 9001, 9502, '后端/安全方向,熟悉 Python 与 FastAPI,写过爬虫和自动化工具;对网络协议和攻防原理有兴趣,持续在 GitHub 上维护自己的小项目。'),
  (95003, 9001, 9503, '想加入技术部和有经验的人一起做真实产品,把课上学的东西用起来,同时补上工程协作的短板。'),
  (95004, 9001, 9504, 'Python/FastAPI、MySQL、Redis 基础;会写 pytest 测试;了解 Docker 基本使用;前端只会一点 React。工具链:Git/GitHub Actions、Linux。'),
  (95005, 9001, 9505, '1) cyber-stray(https://github.com/Zewang0217/cyber-stray):个人安全工具集合,Python 编写,包含子域名收集与漏洞 PoC 复现脚本,有完整的测试和文档。\n2) myloop-meta(https://github.com/Zewang0217/myloop-meta):一个元循环解释器实验项目,实现了词法分析和求值器,用来理解解释器的工作原理。\n两个项目都在 GitHub 开源,提交记录完整。');

-- 9502 林雪:敷衍样例 → 绝对卡硬 0 → 0 分/初筛不过队列(不自动拒)
INSERT INTO resume_field_value (value_id, resume_id, field_id, field_value) VALUES
  (95011, 9002, 9501, '111'),
  (95012, 9002, 9502, '无'),
  (95013, 9002, 9503, '同上'),
  (95014, 9002, 9504, '请填写技术栈'),
  (95015, 9002, 9505, '没有');

-- 9503 张远:中等无仓 → 引导题 + 奖项 DDG 现查
INSERT INTO resume_field_value (value_id, resume_id, field_id, field_value) VALUES
  (95021, 9003, 9501, '人工智能专业,做过一年数据分析助理,帮社团整理过招新数据。'),
  (95022, 9003, 9502, '想找个能持续做项目的团队,顺便把机器学习用到真实场景。'),
  (95023, 9003, 9503, 'Python/pandas/sklearn 基础,用过 Tableau 做可视化。'),
  (95024, 9003, 9504, '给学院做过一份招新转化率分析,用 pandas 清洗了两千条报名数据,输出可视化周报。'),
  (95025, 9003, 9505, '蓝桥杯上海赛区一等奖;校级程序设计竞赛二等奖。');

-- 9504 刘淇:无证据典型 → 基础三维兜底 + 部门技能题组
INSERT INTO resume_field_value (value_id, resume_id, field_id, field_value) VALUES
  (95031, 9004, 9501, '统计学专业,喜欢把数据变成别人能看懂的图表。'),
  (95032, 9004, 9502, '媒体部的数据运营方向很吸引我,想让内容更有数据支撑。'),
  (95033, 9004, 9503, 'Excel 很熟,在学 SQL 和 Python;做过两万粉账号的内容运营。'),
  (95034, 9004, 9504, '运营过 2w 粉账号,策划过三场线上活动,最高单场参与两千人。'),
  (95035, 9004, 9505, '暂无竞赛获奖。');

-- ── 自我介绍字段(字段 5)的敷衍样例(林雪)一并覆盖,保证绝对卡成立 ──────
UPDATE resume_field_value SET field_value = '111'
WHERE resume_id = 9002 AND field_id = 5;
