# -*- coding: utf-8 -*-
"""MatrixOne（MO）向量数据库操作封装。

负责：
- 建立连接
- 创建 view_pic_vec / view_text_vec 两张向量表
- 写入图片向量、文本分段向量
"""

from typing import List, Sequence

import pymysql

import config


def connect() -> pymysql.connections.Connection:
    """建立到 MO 的连接。"""
    return pymysql.connect(
        host=config.MO_CONFIG["host"],
        port=config.MO_CONFIG["port"],
        user=config.MO_CONFIG["user"],
        password=config.MO_CONFIG["password"],
        db=config.MO_CONFIG["db"],
        charset=config.MO_CONFIG["charset"],
    )


def vec_to_literal(vec: Sequence[float]) -> str:
    """把向量转换成 MO 可识别的向量字面量字符串，如 '[0.1,0.2,0.3]'。"""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def create_tables(conn: pymysql.connections.Connection) -> None:
    """创建两张向量表（若不存在）。

    view_pic_vec : 图纸名称、图片名称、图片向量
    view_text_vec: 图纸名称、分段名称、文本分段向量
    """
    dim = config.VECTOR_DIM
    cursor = conn.cursor()

    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS `{config.PIC_VEC_TABLE}` (
            `diagram_name` varchar(255) DEFAULT NULL,
            `pic_name` varchar(255) DEFAULT NULL,
            `vec_value` vecf64({dim}) DEFAULT NULL,
            KEY `idx_pic_vec` USING ivfflat (`vec_value`)
                lists = 1 op_type 'vector_l2_ops'
        )
        """
    )

    cursor.execute(
        f"""
        CREATE TABLE IF NOT EXISTS `{config.TEXT_VEC_TABLE}` (
            `diagram_name` varchar(255) DEFAULT NULL,
            `chunk_name` varchar(255) DEFAULT NULL,
            `chunk_text` text DEFAULT NULL,
            `vec_value` vecf64({dim}) DEFAULT NULL,
            KEY `idx_text_vec` USING ivfflat (`vec_value`)
                lists = 1 op_type 'vector_l2_ops'
        )
        """
    )
    # 兼容旧表：早期 view_text_vec 没有 chunk_text 列，补加之（已存在则忽略）。
    try:
        cursor.execute(
            f"ALTER TABLE `{config.TEXT_VEC_TABLE}` ADD COLUMN `chunk_text` text DEFAULT NULL"
        )
    except Exception:  # noqa: BLE001  列已存在等情况直接忽略
        pass
    conn.commit()


def delete_diagram_rows(
    conn: pymysql.connections.Connection, table: str, diagram_name: str
) -> None:
    """删除某张图纸在指定表中的已有数据，使脚本可以重复运行而不产生重复行。"""
    cursor = conn.cursor()
    cursor.execute(
        f"DELETE FROM `{table}` WHERE `diagram_name` = %s", (diagram_name,)
    )
    conn.commit()


def insert_pic_vec(
    conn: pymysql.connections.Connection,
    diagram_name: str,
    pic_name: str,
    vec: Sequence[float],
) -> None:
    """写入一条图片向量记录。"""
    cursor = conn.cursor()
    cursor.execute(
        f"INSERT INTO `{config.PIC_VEC_TABLE}` "
        f"(`diagram_name`, `pic_name`, `vec_value`) VALUES (%s, %s, %s)",
        (diagram_name, pic_name, vec_to_literal(vec)),
    )
    conn.commit()


def insert_text_vec(
    conn: pymysql.connections.Connection,
    diagram_name: str,
    chunk_name: str,
    chunk_text: str,
    vec: Sequence[float],
) -> None:
    """写入一条文本分段向量记录（含分段原文 chunk_text）。"""
    cursor = conn.cursor()
    cursor.execute(
        f"INSERT INTO `{config.TEXT_VEC_TABLE}` "
        f"(`diagram_name`, `chunk_name`, `chunk_text`, `vec_value`) "
        f"VALUES (%s, %s, %s, %s)",
        (diagram_name, chunk_name, chunk_text, vec_to_literal(vec)),
    )
    conn.commit()
