# MO 数据库创建表语句样例：
CREATE TABLE `product_price` (
  `项目名称` varchar(255) DEFAULT NULL,
  `单位` varchar(255) DEFAULT NULL,
  `物料编码` varchar(255) DEFAULT NULL,
  `物料描述` varchar(255) DEFAULT NULL,
  `物料单位` varchar(255) DEFAULT NULL,
  `平均单价（元）` varchar(255) DEFAULT NULL,
  `最高价（元）` varchar(255) DEFAULT NULL,
  `最低价（元）` varchar(255) DEFAULT NULL,
  `project_name_embedding` vecf64(1024) DEFAULT NULL,
  `product_embedding` vecf64(1024) DEFAULT NULL,
  KEY `idx01` USING ivfflat (`product_embedding`) lists = 1  op_type 'vector_l2_ops' ,
  KEY `idx02` USING ivfflat (`project_name_embedding`) lists = 1  op_type 'vector_l2_ops' 
)

其中 project_name_embedding 字段和 product_embedding 字段就是向量数据类型

# 使用 python 代码实现对向量字段赋值样例

## 使用 DashScope text-embedding-v4 的样例代码
import os
import dashscope
from http import HTTPStatus

dashscope.api_key = os.getenv("DASHSCOPE_API_KEY") 

resp = dashscope.TextEmbedding.call(
    model="text-embedding-v4",
    input='衣服的质量杠杠的，很漂亮，不枉我等了这么久啊，喜欢，以后还来这里买',
    dimension=1024,  # 指定向量维度
    output_type="dense&sparse"  # 可选：同时输出稠密向量和稀疏向量
)

if resp.status_code == HTTPStatus.OK:
    print(resp)
else:
    print(f"调用失败: {resp.code}, {resp.message}")

## 连接 MO 数据库并插入数据的样例代码

import pymysql

db = pymysql.connect(
        host='freetier-01.cn-hangzhou.cluster.matrixonecloud.cn',
        port=6001,
        user='019ecf67-a14b-742f-b40f-d7ede598bbaf:admin:accountadmin',
        password = "User@123",
        db='zhongding',
        )

cursor = db.cursor()


cursor.execute("SELECT VERSION()")

cursor.execute(`INSERT INTO tbl_name (a,b,c) VALUES(1,2,3), (4,5,6), (7,8,9)`);


data = cursor.fetchone()
print ("Database version : %s " % data)


db.close()

# 相似度查询的样例代码
取项目名称和物料描述最接近实际值的前三行（这里的逻辑是衡量项目名称是否接近，然后再衡量物料描述是否接近）

SELECT
  `项目名称`,
  `物料短描述`,
  `物料单位`,
  `平均单价（元）`,
  `最高价（元）`,
  `最低价（元）`,
  l2_distance(`project_name_embedding`, 此处是原始项目名称经 DashScope text-embedding-v4 向量化后的数值) AS `项目名称相似度距离`,
  l2_distance(`product_embedding`, 此处是原始物料描述经 DashScope text-embedding-v4 向量化后的数值) AS `物料描述相似度距离`
FROM `supply_chain_history`.`product_price`
WHERE `product_embedding` IS NOT NULL
  AND `project_name_embedding` IS NOT NULL
ORDER BY `相似度距离` ASC, `物料描述相似度距离` ASC
LIMIT 3;


