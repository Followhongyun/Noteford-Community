import zstandard as zstd

from noteford.decode import decode_message


def test_group_announcement_plain_snapshot_preserves_paragraphs():
    xml = '<msg><appmsg><title>旧版提示</title><textannouncement><![CDATA[群规则\n\n1. 保持友善\n2. 数值 < 3]]></textannouncement></appmsg></msg>'
    result = decode_message((87 << 32) | 49, 'wxid_sender:\n' + xml, is_group=True)
    assert result.kind == 'group_announcement'
    assert result.title == '群公告'
    assert result.text == '群规则\n\n1. 保持友善\n2. 数值 < 3'
    assert result.sender_override == 'wxid_sender'


def test_group_announcement_rich_fallback_excludes_transport_metadata():
    rich = '<group_notice_item><datalist><dataitem datatype="2"><cdn_datakey>SECRET</cdn_datakey></dataitem><dataitem datatype="1"><datadesc>公告正文</datadesc></dataitem></datalist><source><fromusr>PRIVATE</fromusr></source></group_notice_item>'
    xml = '<appmsg><announcement><![CDATA[' + rich + ']]></announcement></appmsg>'
    result = decode_message((87 << 32) | 49, '', zstd.ZstdCompressor().compress(xml.encode()))
    assert result.text == '公告正文'
    assert result.url is None
    for xml in ['<msg><appmsg/></msg>', '<msg><appmsg><announcement>broken</announcement></appmsg></msg>']:
        result = decode_message((87 << 32) | 49, xml)
        assert result.text == '公告正文尚未同步，请在微信查看。'


def test_decode_zstd_group_text_and_sender():
    raw = zstd.ZstdCompressor().compress('wxid_sender:\nhello'.encode('utf-8'))
    decoded = decode_message(1, raw, is_group=True)
    assert decoded.kind == 'text'
    assert decoded.sender_override == 'wxid_sender'
    assert decoded.text == 'hello'


def test_decode_app_link_title_and_url():
    xml = b'<msg><appmsg><title>Example</title><des>Desc</des><url>https://example.test</url></appmsg></msg>'
    local_type = (5 << 32) | 49
    decoded = decode_message(local_type, xml)
    assert decoded.kind == 'link'
    assert decoded.title == 'Example'
    assert decoded.url == 'https://example.test'
    assert 'Example' in decoded.text


def test_decode_image_md5_from_packed_info():
    md5 = b'0123456789abcdef0123456789abcdef'
    nested = bytes([0x22, len(md5)]) + md5
    packed = bytes([0x08, 0x01, 0x10, 0x02, 0x1A, len(nested)]) + nested
    decoded = decode_message(3, b'', packed_info_data=packed)
    assert decoded.kind == 'image'
    assert decoded.media_md5 == md5.decode('ascii')


def test_channel_video_uses_finder_metadata_not_upgrade_prompt():
    xml = (
        'wxid_sender:\n'
        '<?xml version="1.0"?><msg><appmsg>'
        '<title>当前微信版本不支持展示该内容，请升级至最新版本。</title>'
        '<type>51</type><finderFeed>'
        '<nickname><![CDATA[Creator]]></nickname>'
        '<feeddesc><![CDATA[Useful video description]]></feeddesc>'
        '</finderFeed></appmsg></msg>'
    ).encode('utf-8')
    decoded = decode_message((51 << 32) | 49, xml, is_group=True)
    assert decoded.kind == 'channel_video'
    assert decoded.title == 'Creator'
    assert decoded.text == 'Useful video description'
    assert '升级' not in decoded.text


def test_link_and_channel_extract_visual_metadata():
    link_xml = b'''<msg><appmsg><title>Article</title><type>5</type><url>https://mp.weixin.qq.com/s/x</url><thumburl>https://mmbiz.qpic.cn/cover.jpg</thumburl></appmsg></msg>'''
    link = decode_message((5 << 32) | 49, link_xml)
    assert link.kind == 'link'
    assert link.thumbnail_url == 'https://mmbiz.qpic.cn/cover.jpg'
    assert link.cover_url == 'https://mmbiz.qpic.cn/cover.jpg'

    channel_xml = b'''<msg><appmsg><type>51</type><finderFeed><nickname>Creator</nickname><username>finder_user</username><avatar>https://wx.qlogo.cn/avatar.jpg</avatar><coverUrl>https://wxapp.tc.qq.com/cover.jpg</coverUrl><feeddesc>Video desc</feeddesc></finderFeed></appmsg></msg>'''
    channel = decode_message((51 << 32) | 49, channel_xml)
    assert channel.kind == 'channel_video'
    assert channel.title == 'Creator'
    assert channel.author_username == 'finder_user'
    assert channel.avatar_url == 'https://wx.qlogo.cn/avatar.jpg'
    assert channel.cover_url == 'https://wxapp.tc.qq.com/cover.jpg'


def test_quote_image_is_structured_without_raw_xml():
    xml = '''<msg><appmsg><title>大家患opc多久了</title><type>57</type><refermsg><type>3</type><displayname>超级峰</displayname><createtime>1789983799</createtime><content>&lt;msg&gt;&lt;img md5="0680d8e98a5b8e9bb2227e837b356911" /&gt;&lt;/msg&gt;</content></refermsg></appmsg></msg>'''
    decoded = decode_message((57 << 32) | 49, xml)
    assert decoded.kind == 'quote'
    assert decoded.text == '大家患opc多久了'
    assert decoded.quote_kind == 'image'
    assert decoded.quote_text == '[图片]'
    assert decoded.quote_sender_name == '超级峰'
    assert decoded.quote_media_md5 == '0680d8e98a5b8e9bb2227e837b356911'
    assert decoded.quote_timestamp == 1789983799
    assert '<msg' not in decoded.text


def test_system_xml_is_rendered_as_human_text():
    pinned = '''<sysmsg type="mmchatroomtopmsg"><mmchatroomtopmsg><op>1</op><nickname>Cell 细胞</nickname></mmchatroomtopmsg></sysmsg>'''
    decoded = decode_message(10000, pinned)
    assert decoded.text == 'Cell 细胞 置顶了一条消息'
    assert '<sysmsg' not in decoded.text

    revoked = '''<sysmsg type="revokemsg"><revokemsg><content>"超级峰" 撤回了一条消息</content></revokemsg></sysmsg>'''
    decoded = decode_message(10000, revoked)
    assert decoded.text == '"超级峰" 撤回了一条消息'


def test_sysmsgtemplate_substitutes_member_names():
    xml = '''<sysmsg type="sysmsgtemplate"><sysmsgtemplate><content_template><template>"$username$"邀请"$names$"加入了群聊</template><link_list><link name="username"><memberlist><member><nickname>Miles Ma</nickname></member></memberlist></link><link name="names"><memberlist><member><nickname>挽风</nickname></member></memberlist></link></link_list></content_template></sysmsgtemplate></sysmsg>'''
    decoded = decode_message(10000, xml)
    assert 'Miles Ma' in decoded.text
    assert '挽风' in decoded.text
    assert '$username$' not in decoded.text


def test_emoji_extracts_cdn_and_md5():
    xml = b'''<msg><emoji md5="b02255daa9d1f5b37def778b8b1f498f" cdnurl="http://vweixinf.tc.qq.com/emoji.png" desc="" /></msg>'''
    decoded = decode_message(47, xml)
    assert decoded.kind == 'emoji'
    assert decoded.media_md5 == 'b02255daa9d1f5b37def778b8b1f498f'
    assert decoded.thumbnail_url == 'http://vweixinf.tc.qq.com/emoji.png'
    assert decoded.text == '[表情]'


def test_transfer_uses_amount_instead_of_upgrade_prompt():
    xml = '''<msg><appmsg><title>微信转账</title><des>收到转账4.00元。如需收钱，请点此升级至最新版本</des><type>2000</type><wcpayinfo><feedesc>￥4.00</feedesc><pay_memo></pay_memo></wcpayinfo></appmsg></msg>'''
    decoded = decode_message((2000 << 32) | 49, xml)
    assert decoded.kind == 'transfer'
    assert decoded.title == '￥4.00'
    assert decoded.text == '微信转账'
    assert '升级' not in decoded.text


def test_gif_extracts_remote_url_from_emojiinfo():
    import base64

    url = "http://vweixinf.tc.qq.com/110/20401/stodownload?m=abc123&bizid=1022"
    payload = b"\x0a\x20deadbeefdeadbeefdeadbeefdeadbeef" + url.encode("ascii")
    encoded = base64.b64encode(payload).decode("ascii")
    xml = (
        "<msg><appmsg><type>8</type><appattach>"
        "<emoticonmd5>af41ef809e1155f7806a1c464f25df1e</emoticonmd5>"
        f"<emojiinfo>{encoded}</emojiinfo>"
        "</appattach></appmsg></msg>"
    )
    decoded = decode_message((8 << 32) | 49, xml)
    assert decoded.kind == "gif"
    assert decoded.media_md5 == "af41ef809e1155f7806a1c464f25df1e"
    assert decoded.thumbnail_url == url
    assert decoded.text == "[表情]"


def test_generic_app_never_surfaces_nested_xml_markup():
    xml = (
        "<msg><appmsg><title>正常标题</title><type>24</type>"
        "<des>&lt;msg&gt;&lt;img md5='abc' /&gt;&lt;/msg&gt;</des>"
        "</appmsg></msg>"
    )
    decoded = decode_message((24 << 32) | 49, xml)
    assert decoded.kind == "note"
    assert decoded.title == "正常标题"
    assert "<msg" not in decoded.text
    assert "<img" not in decoded.text
def test_solitaire_uses_readable_outer_title_not_internal_ranges():
    from noteford.decode import decode_message
    body = '#接龙\n周末活动报名\n请填写姓名和人数\n\n1. 示例甲 2人\n2. 示例乙 1人'
    xml = f'<msg><appmsg><title><![CDATA[{body}]]></title><solitaire_info><![CDATA[<solitaire><tt>0-114</tt><ex>0-0</ex><content><i><r>119-11</r><c>示例甲</c></i></content></solitaire>]]></solitaire_info></appmsg></msg>'
    result = decode_message((53 << 32) | 49, xml)
    assert result.text == body
    assert '119-11' not in result.text
    assert '0-114' not in result.text


def test_solitaire_missing_body_does_not_display_internal_ranges():
    from noteford.decode import decode_message
    xml = '<msg><appmsg><solitaire_info><![CDATA[<solitaire><tt>0-114</tt></solitaire>]]></solitaire_info></appmsg></msg>'
    result = decode_message((53 << 32) | 49, xml)
    assert result.text == '接龙正文尚未同步，请在微信查看。'

def test_payment_metadata_is_explicit_and_identity_scoped():
    from noteford.repository import _payment_metadata
    xml = '<msg><appmsg><wcpayinfo><receiver_username>recipient</receiver_username><invalidtime>1791800000</invalidtime></wcpayinfo></appmsg></msg>'
    transfer = decode_message((2000 << 32) | 49, xml)
    assert _payment_metadata(transfer, 'recipient') == {'recipient_is_self': True, 'expires_at': 1791800000}
    assert _payment_metadata(transfer, 'other')['recipient_is_self'] is False
    assert _payment_metadata(transfer, None)['recipient_is_self'] is None
    packet = decode_message((2001 << 32) | 49, xml)
    assert packet.payment_expires_at == 1791800000
    for value in ('0', '-1', '1791800000000', 'tomorrow'):
        assert decode_message((2001 << 32) | 49, xml.replace('1791800000', value)).payment_expires_at is None
    unrelated = '<msg><appmsg><title>expired</title><refermsg><wcpayinfo><receiver_username>other</receiver_username><invalidtime>1791800000</invalidtime></wcpayinfo></refermsg></appmsg></msg>'
    unknown = decode_message((2000 << 32) | 49, unrelated)
    assert _payment_metadata(unknown, 'recipient') == {'recipient_is_self': None, 'expires_at': None}

def test_red_envelope_eligibility_uses_only_exclusive_recipient():
    from noteford.repository import _payment_metadata
    def packet(fields):
        return decode_message((2001 << 32) | 49, '<msg><appmsg><wcpayinfo>'+fields+'</wcpayinfo></appmsg></msg>')
    exclusive = packet('<exclusive_recv_username>wxid_other</exclusive_recv_username>')
    assert _payment_metadata(exclusive, 'wxid_me')['recipient_is_self'] is False
    assert _payment_metadata(exclusive, 'wxid_other')['recipient_is_self'] is True
    assert _payment_metadata(exclusive, None)['recipient_is_self'] is None
    for fields in ('<receiver_username>wxid_other</receiver_username>', '<exclusive_recv_username></exclusive_recv_username>', '<exclusive_recv_username>wxid_a,wxid_b</exclusive_recv_username>', '<receiverdes>已领完</receiverdes>'):
        assert _payment_metadata(packet(fields), 'wxid_me')['recipient_is_self'] is None

def test_solitaire_matching_requires_native_sid():
    def chain(sid_xml, body='报名\n1. 小明', author='starter'):
        return decode_message((53 << 32) | 49, '<msg><appmsg><title><![CDATA['+body+']]></title><extinfo><solitaire_info><![CDATA[<solitaire><au>'+author+'</au>'+sid_xml+'</solitaire>]]></solitaire_info></extinfo></appmsg></msg>')
    first = chain('<sid>native-chain-1</sid>')
    assert first.solitaire_key
    assert chain('<sid>native-chain-1</sid>', '修改说明\n1. 小红\n2. 小明').solitaire_key == first.solitaire_key
    assert chain('<sid>native-chain-2</sid>').solitaire_key != first.solitaire_key
    for missing in ('', '<sid/>', '<sid> </sid>', '<content><sid>native-chain-1</sid></content>', '<sid>a</sid><sid>b</sid>'):
        assert chain(missing).solitaire_key is None
    assert chain('<sid>' + 'a' * 257 + '</sid>').solitaire_key is None
