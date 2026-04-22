# 参考： def _forward_impl(self, x, feature): # The com
_Exported on 04/22/2026 at 17:13:45 GMT+8 from Alibaba Lingma via WayLog_


**User**

参考： def _forward_impl(self, x, feature): # The comment resolution is based on input size is 224*224 imagenet # f.shape: (batch_size, 3, 224, 224), feature.shape: (batch_size, 768) features, weights, parts, weights_org, weights_max = [list() for i in range(5)] for i in range(len(x[0])): features.clear() weights.clear() for j in range(len(x)): f = x[j][i] f = self.conv1(f) f = self.bn1(f) f = self.relu(f) f = self.maxpool(f) f = self.layer1(f) f = self.layer2(f) f = self.layer3(f) f = self.layer4(f) f = self.avgpool(f) f = torch.flatten(f, 1) # features.append(f) features.append(torch.cat([f, feature], dim=1)) # concat regional feature with global feature weights.append(self.get_weight(features[-1])) features_stack = torch.stack(features, dim=2) weights_stack = torch.stack(weights, dim=2) weights_stack = softmax(weights_stack, dim=2) weights_max.append(weights_stack[:, :, :len(x)].max(dim=2)[0]) weights_org.append(weights_stack[:, :, 0]) parts.append(features_stack.mul(weights_stack).sum(2).div(weights_stack.sum(2))) parts_stack = torch.stack(parts, dim=0) out = parts_stack.sum(0).div(parts_stack.shape[0]) pred_score = self.fc(out) return pred_score, weights_max, weights_org 构造真是的数据


---


**Alibaba Lingma**

用户请求构造真实数据以测试两个前向传播实现的结果差异。提供的代码实现了`_forward_impl`和`_forward_impl_best`两个版本的前向传播逻辑，并创建了一个测试文件`test_forward_impl_detailed.py`用于验证两者的输出一致性。该测试文件包含多尺度、多区域的图像数据生成、模型组件模拟、GPU支持以及详细的对比分析。调用的agent类型为`common`。

---
