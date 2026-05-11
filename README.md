**此项目用于测试claude api key的可用性和使用方法**
以下是当前已有key和官方给出的调用方法：
```
您好，您的Claude code API  KEY 是：sk-tj4sP4aqprTftTWUVLEhnw
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
我需要实现能够通过python调用这个api，先做一个简单的demo，实现运行后可以在终端和ai对话