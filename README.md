**此项目用于测试 Claude API key 的可用性和使用方法。**

Python 终端对话：安装依赖后运行 `python3 chat.py`；模型列表：`python3 chat.py --list-models`。将 `config.example.json` 复制为 `config.json` 并填写密钥（`config.json` 已加入 `.gitignore`，请勿提交密钥）。

以下为官方/渠道给出的 Claude Code 配置方式（请将密钥替换为你自己的）：
```
使用方法：
使用官方安装方式安装原版Claude code；
修改Claude code配置文件:
 macOS/Linux系统下的配置文件路径是：~/.claude/settings.json
 Windows系统下配置文件路径是：C:\Users\你的用户名.claude\settings.json
填入或修改成以下内容：
{
  "env": {
 "ANTHROPIC_BASE_URL": "https://c.loonaai.cn",
 "ANTHROPIC_AUTH_TOKEN": "你的key"
  }
}
```