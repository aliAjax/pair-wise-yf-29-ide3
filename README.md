# 法律证据保管与流转后台

仅使用 Python 3.11+ 标准库实现的证据保管项目。支持真实 SHA-256 入册、封存/开箱/移交、分析衍生关系、案件成员权限、法律保留、保留期限、不可变保管事件链、跨案证据引用和 JSON 报告导出。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

访问 <http://127.0.0.1:8105>，默认数据库 `custody.db`。测试：

```bash
python3 -m unittest -v
```

演示身份：`custodian1`、`custodian2`、`analyst1`、`auditor1`、`outsider`。请求使用 `X-User-Id`。

## 主要接口

- `POST /api/cases`：创建案件，创建人自动成为保管员。
- `POST /api/cases/{id}/members`：授予 custodian、analyst 或 auditor 角色。
- `POST /api/cases/{id}/evidence`：以 Base64 入册证据，服务端计算 SHA-256 和大小。
- `GET /api/evidence/{id}`：查看元数据、完整保管事件链、完整性结果和衍生关系。
- `POST /api/evidence/{id}/open`：保管员开箱。
- `POST /api/evidence/{id}/transfer`：移交保管人并记录位置。
- `POST /api/evidence/{id}/derive`：分析员从已开箱证据创建衍生证据。
- `POST /api/evidence/{id}/hold`：审计员或案件创建人设置/解除法律保留。
- `POST /api/evidence/{id}/release`：存在法律保留时拒绝释放。
- `POST /api/evidence/{id}/references`：目标案件成员填写目标案件与用途，申请跨案引用；提交时快照源证据状态（状态、保管人、法律保留、保管链链头哈希）。
- `POST /api/references/{id}/review`：源案件保管员受理（`decision=accept/return`，退回须填原因）。核对时发现源证据已移交、开箱、释放或法律保留变化（或保管链新增事件），即使提交 accept 也强制退回并记录具体原因。
- `POST /api/references/{id}/resubmit`：被退回申请仅由原申请人重提，生成关联原申请的新申请（`resubmit_of`）并刷新快照；同一证据对同一目标案件不允许重复的待受理/已受理引用。
- `GET /api/evidence/{id}`：引用受理后，目标案件成员可只读查看元数据、完整性结果与保管链（`access=reference`、`read_only=true`），不返回证据内容；开箱、移交、派生仍只允许源案件成员。
- `GET /api/cases/{id}/references`：案件双向引用台账（`incoming` 他案引用本案、`outgoing` 本案引用他案），待受理申请实时附带源证据漂移提示。
- `GET /api/cases/{id}/report`：校验所有证据哈希和每条事件链，导出完整报告；`references` 字段列出引用状态与退回原因。
- 所有 `DELETE` 请求返回 405；证据、保管记录和引用申请不提供删除接口。

## 模块分工

引用规则、保管记录与页面展示分开维护：

- `crossref.py`：跨案引用规则（申请快照、受理漂移核对、强制退回、重提、只读查阅）。
- `app.py`：保管记录（入册、开箱、移交、派生、保留、报告）与 HTTP 路由接线。
- `common.py`：共享的业务错误与时间工具。
- `web/index.html`：页面展示，只调用上述接口，不内嵌规则。

保管事件通过前一条事件哈希串联；报告会重新计算文件哈希和事件链。项目适合流程与完整性原型，不涵盖现实中的签名证书、WORM 存储、证据文件加密或司法辖区合规认证。
