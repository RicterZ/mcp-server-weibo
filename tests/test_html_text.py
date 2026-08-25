from mcp_server_weibo.html_text import html_to_text
from mcp_server_weibo.weibo import WeiboCrawler


def test_plain_text_is_unchanged():
    assert html_to_text("9月将是小米的“科技大月”。") == "9月将是小米的“科技大月”。"
    assert html_to_text("") == ""
    assert html_to_text(None) == ""


def test_br_and_expand_link_from_live_feed():
    raw = (
        "小米澎程技术发布会，发布小米昆仑技术架构与两款智能可变大空间SUV。"
        "这两款SUV预计9月正式上市，现已开启小订，欢迎登录小米汽车APP。"
        "<br /><br />小米澎程N90 Max，大七座旗舰增程SUV，预售价29.99万元。"
        "<br />小米澎程N70 Max，大五座四驱增程SUV，预售价25.99万元。"
        "<br />发布会全程视频(1小时58分钟)  ...<a href=\"/status/5326689903314726\">全文</a>"
    )
    assert html_to_text(raw) == (
        "小米澎程技术发布会，发布小米昆仑技术架构与两款智能可变大空间SUV。"
        "这两款SUV预计9月正式上市，现已开启小订，欢迎登录小米汽车APP。\n"
        "\n"
        "小米澎程N90 Max，大七座旗舰增程SUV，预售价29.99万元。\n"
        "小米澎程N70 Max，大五座四驱增程SUV，预售价25.99万元。\n"
        "发布会全程视频(1小时58分钟)  ..."
    )


def test_topic_mention_emoji_and_video_card():
    raw = (
        "今天，小米玄戒三芯正式发布。<br />"
        "<a  href=\"https://m.weibo.cn/search?containerid=231522type%3D1%26t%3D10%26q%3D%23%E5%B0%8F%E7%B1%B3%E7%8E%84%E6%88%92%23\""
        " data-hide=\"\"><span class=\"surl-text\">#小米玄戒#</span></a> "
        "<a  href=\"https://video.weibo.com/show?fid=1034:5335553139474453\" data-hide=\"\">"
        "<span class='url-icon'><img style='width: 1rem;height: 1rem' "
        "src='https://h5.sinaimg.cn/upload/2015/09/25/3/timeline_card_small_video_default.png'></span>"
        "<span class=\"surl-text\">小米公司的微博视频</span></a> "
    )
    assert html_to_text(raw) == (
        "今天，小米玄戒三芯正式发布。\n"
        "#小米玄戒# 小米公司的微博视频"
    )
    assert html_to_text(
        "早安，小米澎程！<span class=\"url-icon\">"
        "<img alt=\"[心]\" src=\"https://face.t.sinajs.cn/t4/appstyle/expression/ext/normal/79/201810_xin_mobile.png\" "
        "style=\"width:1em; height:1em;\" /></span><br /><br />大家上车体验没？ "
    ) == "早安，小米澎程！[心]\n\n大家上车体验没？"


def test_comment_reply_and_web_link():
    raw = (
        "回复<a href='https://m.weibo.cn/n/我假装笑得很开心'>@我假装笑得很开心</a>:"
        "已经申请某多客服介入了。"
    )
    assert html_to_text(raw) == "回复@我假装笑得很开心:已经申请某多客服介入了。"
    assert html_to_text(
        "不知道什么样的问题能满足你保修的规则？ "
        "<a href=\"https://weibo.cn/sinaurl?f=w&amp;u=http%3A%2F%2Ft.cn%2FAXp0bv3L\">网页链接</a>"
    ) == "不知道什么样的问题能满足你保修的规则？ 网页链接"
    assert html_to_text(
        "DS怎么是0%<span class=\"url-icon\">"
        "<img alt=[思考] src=\"https://h5.sinaimg.cn/m/emoticon/icon/default/d_sikao-ff9602dd08.png\" "
        "style=\"width:1em; height:1em;\" /></span>"
    ) == "DS怎么是0%[思考]"


def test_dump_search_content_and_home_timeline_html():
    mobile = (
        '<a  href="https://m.weibo.cn/search?containerid=231522type%3D1%26t%3D10%26q%3D%23%E8%94%9A%E8%93%9D%E6%A1%A3%E6%A1%88%E2%80%AC%23'
        '&extparam=%23%E8%94%9A%E8%93%9D%E6%A1%A3%E6%A1%88%E2%80%AC%23&launchid=10000360-page_H5" data-hide="">'
        '<span class="surl-text">#蔚蓝档案#</span></a> '
        '<a  href="https://m.weibo.cn/p/index?extparam=%E8%94%9A%E8%93%9D%E6%A1%A3%E6%A1%88'
        '&containerid=1008080bc3b7954f3d6b83e0797c22447aea1a&launchid=10000360-page_H5" data-hide="">'
        "<span class='url-icon'><img style='width: 1rem;height: 1rem' "
        "src='https://n.sinaimg.cn/photo/5213b46e/20180926/timeline_card_small_super_default.png'></span>"
        '<span class="surl-text">蔚蓝档案</span></a> <br />【百花缭乱篇】第2章Part2（16~28话）即将更新！'
        "<br />百花缭乱和老师中了陷阱被困，此时新的恐怖正悄然逼近——<br /><br />■ 更新时间"
        "<br />08月27日 14:00后<br /><br />■ 更新剧情<br />第16话《人造花》"
        ' ...<a href="/status/5335755900846129">全文</a>'
    )
    assert html_to_text(mobile) == (
        "#蔚蓝档案# 蔚蓝档案\n"
        "【百花缭乱篇】第2章Part2（16~28话）即将更新！\n"
        "百花缭乱和老师中了陷阱被困，此时新的恐怖正悄然逼近——\n"
        "\n"
        "■ 更新时间\n"
        "08月27日 14:00后\n"
        "\n"
        "■ 更新剧情\n"
        "第16话《人造花》 ..."
    )

    desktop = (
        '<a href="//s.weibo.com/weibo?q=%23%E8%94%9A%E8%93%9D%E6%A1%A3%E6%A1%88%E2%80%AC%23" target="_blank">#蔚蓝档案#</a> '
        '<a target="_blank" href="https://weibo.com/p/1008080bc3b7954f3d6b83e0797c22447aea1a">'
        '<img class="icon-link" title="#蔚蓝档案[超话]#" '
        'src="https://h5.sinaimg.cn/upload/100/959/2020/05/09/timeline_card_small_super_default.png"/>'
        "蔚蓝档案超话</a> <br />【百花缭乱篇】第2章Part2（16~28话）即将更新！"
        '第21话《 ​​​ ...<span class="expand">展开</span>'
    )
    assert html_to_text(desktop) == (
        "#蔚蓝档案# 蔚蓝档案超话\n"
        "【百花缭乱篇】第2章Part2（16~28话）即将更新！"
        "第21话《 ​​​ ..."
    )


def test_feed_and_comment_converters_use_filter():
    crawler = WeiboCrawler()
    feed = crawler._to_feed_item({
        "id": 1,
        "text": "第一行<br />第二行 ...<a href=\"/status/1\">全文</a>",
        "user": {"id": 2, "screen_name": "用户"},
    })
    assert feed.text == "第一行\n第二行 ..."

    comment = crawler._to_comment_item({
        "id": 3,
        "text": "回复<a href='https://m.weibo.cn/n/someone'>@someone</a>:收到",
        "created_at": "刚刚",
        "user": {"id": 2, "screen_name": "用户"},
        "reply_text": "原文<span class=\"url-icon\"><img alt=\"[赞]\" src=\"x.png\" /></span>",
    })
    assert comment.text == "回复@someone:收到"
    assert comment.reply_text == "原文[赞]"
