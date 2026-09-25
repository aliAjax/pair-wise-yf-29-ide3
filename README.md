# 法律证据保管与流转后台

仅使用 Python 3.11+ 标准库实现的证据保管项目。支持真实 SHA-256 入册、封存/开箱/移交、分析衍生关系、案件成员权限、法律保留、保留期限、不可变保管事件链、跨案引用和 JSON 报告导出。

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

## 模块划分（分开维护）

- `records.py` —— **保管记录**：证据、保管人、封存/开箱/移交、衍生关系、法律保留和前哈希串联的不可变保管事件链。
- `references.py` —— **跨案引用规则**：申请、受理核对、退回与重提的状态机和权限规则，不持有证据内容、不修改源证据。
- `app.py` —— HTTP 接口装配层，组合上面两个模块。
- `web/index.html` —— **页面展示**：报告、发起引用、受理核对、退回重提的独立静态页，不承载业务规则。

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
- `GET /api/cases/{id}/report`：校验所有证据哈希和每条事件链，导出完整报告（含 `references` 引用章节）。
- 所有 `DELETE` 请求返回 405；证据和保管记录不提供删除接口。

### 跨案引用

- `POST /api/references`：**目标案件成员**发起引用，提交 `evidence_id`、`target_case_id`、`purpose`。不能引用本案件证据。
- `POST /api/references/{id}/review`：**源案件保管员**核对，`decision` 为 `accept` 或 `return`（退回需 `reason`）。
  - 受理瞬间若源证据自申请快照后已**移交**（保管人变化）、**开箱**（状态离开封存，含释放）或**法律保留变化**，系统自动退回并写明具体原因；
  - 退回后由申请人用 `POST /api/references/{id}/resubmit` 在目标案件重新提交（新单据保留 `attempt` 和原单据关联）。
- `GET /api/references/{id}`：源案件或目标案件成员查看申请；待受理时实时返回 `verification_changes`。
- `GET /api/references/{id}/evidence`：受理后目标案件**只读**查看源证据元数据与保管链；不返回原件内容，目标案件不能开箱、移交或派生。
- `GET /api/cases/{id}/references?direction=incoming|outgoing`：来件（别人引用本案）或出件（本案引用别人）。
- 报告的 `references` 章节列出每条引用的状态、退回原因、第几次提交，并标注受理后源证据是否又发生变化。

保管事件通过前一条事件哈希串联；报告会重新计算文件哈希和事件链。项目适合流程与完整性原型，不涵盖现实中的签名证书、WORM 存储、证据文件加密或司法辖区合规认证。
