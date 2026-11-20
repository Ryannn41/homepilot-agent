from app.agent import split_sentences


def test_split_sentences():
    assert split_sentences("你好！今天晴天。明天") == (["你好！", "今天晴天。"], "明天")
    assert split_sentences("真的吗？？好的\n") == (["真的吗？？", "好的"], "")
    assert split_sentences("还没说完") == ([], "还没说完")
    assert split_sentences("\n\n") == ([], "")
