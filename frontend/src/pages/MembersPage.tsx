// 用户管理（仅 admin）：新建 reviewer/operator、停用、分页列表。
//
// 与 backend RBAC 对齐的两条硬约束（页面直接体现，避免「点了才报 403」）：
//   · 只能创建 `reviewer` / `operator`（`admin` 只能由注册流程产生 —— 防提权）；
//   · 停用是单向的（后端无「启用」接口），确认框里要讲明白。
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button, Form, Input, message, Modal, Popconfirm, Select, Space, Table, Tag, Typography } from "antd";
import { useState } from "react";

import { membersApi } from "../api";
import { apiErrorMessage } from "../services/errors";
import { ROLE_LABEL } from "../store/authStore";
import type { Member } from "../types/api";

interface CreateValues {
  username: string;
  password: string;
  role: "reviewer" | "operator";
}

export default function MembersPage() {
  const qc = useQueryClient();
  const [creating, setCreating] = useState(false);
  const [page, setPage] = useState(1);
  const pageSize = 10;
  const { data, isLoading } = useQuery({
    queryKey: ["members", page],
    queryFn: () => membersApi.list({ offset: (page - 1) * pageSize, limit: pageSize }),
  });

  const create = useMutation({
    mutationFn: (values: CreateValues) => membersApi.create(values),
    onSuccess: (m) => {
      message.success(`成员已创建：${m.username}（${ROLE_LABEL[m.role] ?? m.role}）`);
      setCreating(false);
      qc.invalidateQueries({ queryKey: ["members"] });
    },
    onError: (e) => message.error(apiErrorMessage(e, "创建失败")),
  });

  const disable = useMutation({
    mutationFn: (id: string) => membersApi.disable(id),
    onSuccess: () => {
      message.success("成员已停用（该账号将无法登录）");
      qc.invalidateQueries({ queryKey: ["members"] });
    },
    onError: (e) => message.error(apiErrorMessage(e, "停用失败")),
  });

  const columns = [
    { title: "用户名", dataIndex: "username" },
    { title: "角色", dataIndex: "role", render: (v: string) => <Tag>{ROLE_LABEL[v] ?? v}</Tag> },
    {
      title: "状态",
      dataIndex: "status",
      render: (v: string) => <Tag color={v === "active" ? "green" : "default"}>{v === "active" ? "正常" : "已停用"}</Tag>,
    },
    {
      title: "最近登录",
      dataIndex: "last_login_at",
      render: (v: string | null | undefined) => v ?? <Typography.Text type="secondary">从未登录</Typography.Text>,
    },
    {
      title: "操作",
      render: (_: unknown, row: Member) => (
        <Space>
          <Popconfirm
            title="确认停用该成员？"
            description="停用后该账号无法登录，且当前没有「重新启用」接口（单向操作）。"
            okText="停用"
            okButtonProps={{ danger: true }}
            onConfirm={() => disable.mutate(row.id)}
            disabled={row.status !== "active"}
          >
            <Button size="small" danger disabled={row.status !== "active"}>
              停用
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <div>
      <Space style={{ marginBottom: 12, justifyContent: "space-between", width: "100%" }}>
        <Typography.Title level={4} style={{ margin: 0 }}>
          用户管理
        </Typography.Title>
        <Button type="primary" onClick={() => setCreating(true)}>
          新增成员
        </Button>
      </Space>
      <Table<Member>
        rowKey="id"
        loading={isLoading}
        columns={columns}
        dataSource={data?.items ?? []}
        pagination={{
          current: page,
          pageSize,
          total: data?.total ?? 0,
          onChange: setPage,
          showSizeChanger: false,
        }}
      />
      <Modal
        title="新增成员"
        open={creating}
        onCancel={() => setCreating(false)}
        footer={null}
        destroyOnHidden
      >
        <Form<CreateValues>
          layout="vertical"
          onFinish={(values) => create.mutate(values)}
          initialValues={{ role: "operator" }}
        >
          <Form.Item label="用户名" name="username" rules={[{ required: true, min: 3, message: "至少 3 位" }]}>
            <Input placeholder="用户名" />
          </Form.Item>
          <Form.Item label="初始密码" name="password" rules={[{ required: true, min: 8, message: "至少 8 位" }]}>
            <Input.Password placeholder="至少 8 位" />
          </Form.Item>
          <Form.Item
            label="角色"
            name="role"
            tooltip="admin 只能由注册流程产生（防提权）：这里只能创建审核员或运营"
            rules={[{ required: true }]}
          >
            <Select
              options={[
                { value: "reviewer", label: "审核员（可审批）" },
                { value: "operator", label: "运营（仅操作，不可审批）" },
              ]}
            />
          </Form.Item>
          <Button type="primary" htmlType="submit" block loading={create.isPending}>
            创建
          </Button>
        </Form>
      </Modal>
    </div>
  );
}
