from app.agent import split_sentences


def test_split_sentences():
    assert split_sentences("你好！今天晴天。明天") == (["你好！", "今天晴天。"], "明天")
    assert split_sentences("真的吗？？好的\n") == (["真的吗？？", "好的"], "")
    assert split_sentences("还没说完") == ([], "还没说完")
    assert split_sentences("\n\n") == ([], "")


def test_split_sentences_keeps_closing_quotes():
    text = "老师问：“什么越洗越脏？”小明答：“水。”（笑）……"
    assert split_sentences(text) == (["老师问：“什么越洗越脏？”", "小明答：“水。”"], "（笑）……")
    assert split_sentences("好的。”\n") == (["好的。”"], "")


def test_split_sentences_drops_punctuation_only():
    assert split_sentences("你好。\n……！\n") == (["你好。"], "")
