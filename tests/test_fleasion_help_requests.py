import pytest

from moist_bot.cogs.fleasion import HelpMessageClassifier, HelpMessageEnum


@pytest.mark.parametrize(
    'content',
    [
        'yoo anyone could help me i js want to search up a game but it dont show up',
        'guys can i have help',
        'chat how do i use fleasion',
        'Chat, how do I use Fleasion?',
        'yo chat is fleasion working?',
        'can i have help bc when i download the skins i cant see the skin',
        'Anyone know how to remove tree leaves?',
        'do anybody of you guys know how to replace the normal arm?',
        'only thing i dont know is, how to replace the arm',
        'anyone here can help?',
        'so how do i do it',
        'yo i cannot get fleasion going idk how to do the configs',
        'could i get sum help with working fleasion?',
        'is there a vid tuto on how to make ur own config',
        'DM IF YOU CAN HELP ME',
        'who can help me model i am new',
        'yo can someone dm me how to make obj to jsons?',
        'oh, okay, does anyone know how to put the shift in the middle?',
        'also how do i make my shotgun behave',
        'why do the models not work and idk how to use the .obj file',
        'do you know how to fix this?',
        'is there an video i can watch to learn how to do it?',
        'yo can somone help me im kinda new to fleasion',
        '<@123> What do I do if its quarantined?',
        '<@!123> <@456> What do I do, I downloaded it',
        'hi guys need some help',
        'my textures do not load, any help would be appreciated',
        'need helpppppppp',
        'also, is it okay if I ask for help here?',
        'yo guys can someone tell me how to change obj to json?',
        'im help me',
        'who can help with this config',
        'yo do yk how to turn on fflags?',
        'what does this mean',
        'Boi help me',
        'i rlly need help',
        'what do i put on profiel name',
        'yo some1 js help brs///',
        'can anyone join a call with me and show me how to do fleasion',
        'damn bru i js need help doin this',
        'How to download skinchanger on mobile',
        'yo\ncan anyone help?\ni need lower ping',
        'help',
        'I don’t know how to import this config',
        'why is fleason not working on mac anymore???',
        'is fleasion working??',
        'yo is fleasion still working on windows?',
        'does fleason work on linux',
        'is fleasion on mac not working?',
        'why does fleasion crash when i launch roblox?',
        "why isn't fleasion opening?",
        'why doesnt fleason load',
        'is fleasion down rn?',
        'is fleasion broken?',
        'anyone know if fleasion is working?',
        'does anyone know why fleason is not working?',
        'any1 know if fleasion works on mac?',
        'fleasion not working anymore',
        'my fleasion is not loading',
        'fleason won’t open',
        'fleasion stopped working after the update',
        'fleasion keeps crashing',
        'why fleasion not working',
        '<@123> IS FLEASION WORKING',
    ],
)
def test_detects_help_requests(content: str) -> None:
    assert HelpMessageClassifier.BINDINGS[HelpMessageEnum.HELP](content)


@pytest.mark.parametrize(
    'greeting',
    [
        'yall',
        "y'all",
        'y’all',
        'everyone',
        'anybody',
        'folks',
        'people',
        'gang',
        'team',
        'hello',
        'hiya',
        'sup',
        'ayo',
        'ey',
        'oi',
        'dude',
        'man',
        'mate',
        'bros',
        'bruv',
    ],
)
def test_greeting_prefixes(greeting: str) -> None:
    assert HelpMessageClassifier.BINDINGS[HelpMessageEnum.HELP](
        f'{greeting} how do i use fleasion'
    )
    assert HelpMessageClassifier.BINDINGS[HelpMessageEnum.HELP](
        f'{greeting.upper()}, is fleasion working?'
    )
    assert not HelpMessageClassifier.BINDINGS[HelpMessageEnum.HELP](greeting)
    assert not HelpMessageClassifier.BINDINGS[HelpMessageEnum.HELP](
        f'{greeting} that was a fun game'
    )


@pytest.mark.parametrize(
    'content',
    [
        'mb no one i on help chat',
        'like why are we rude',
        'why is support chat not helping with anything bro',
        'This server lowk dead now, I use to help here but I left..',
        "How are you a fan of Fleasion but don't know how to use it",
        'not helpful',
        'thanks for the help',
        'thanks for helping me',
        'I can help you',
        'I know how to download it',
        'I used to need help with this',
        'helpful advice',
        'I wrote a guide on how to install this',
        'you should ask someone who knows how to fix it',
        'Can someone tell me a joke?',
        'Use <#123>. **Please do not ask for help here.**',
        'fleasion is working fine for me',
        'fleason works on my pc',
        'fleasion is not broken',
        'I fixed fleasion not working yesterday',
        'I made a tutorial about why fleasion is not working',
        'the fleasion team is working on an update',
        'is fleasion support working on the issue?',
        '',
    ],
)
def test_ignores_conversation(content: str) -> None:
    assert not HelpMessageClassifier.BINDINGS[HelpMessageEnum.HELP](content)


@pytest.mark.parametrize(
    'content',
    [
        'does anyone have the best potato graphics config',
        'can anyone give me fflags config',
        'does anb have a decent prison life config for fleasion',
        'yo does anyone have a dark textures config?',
        'can any1 share your grenade launcher macro cfg i keep selecting wrong wep',
        'Anyone know what cfg the streamer is using rn?',
        'Anyone know the cfg for rivals where headshots sound like bubbles?',
        'anyone have criminality cfg or guns?',
        'make me boneclaw rifle cfg with that PLEASE',
        'i was lowkey looking for this cfg over an hour',
        'any1 got a good cfg w no textures and skins plus a skybox for rivals',
        'is there a slingshot bwai charm over rivals charm cnfg?',
        '-can someone give me no iron sight cnfg in pf',
        'does anyone has a cnfg for it?',
        'can someone give me a cnfg to delete the scope model',
        'can someone send me good cnfg for low fps',
        'Can someone send me blurry arena cnfg',
        'i need a config for rivals',
        'could i get a cfg please',
        'where can i find a good config?',
        'anyone have configs?',
        'anyone have cfgs?',
        'anyone have cnfgs?',
        'CAN ANY1 SHARE A CFG?',
    ],
)
def test_detects_config_requests(content: str) -> None:
    assert HelpMessageClassifier.BINDINGS[HelpMessageEnum.CONFIG](content)


@pytest.mark.parametrize(
    'content',
    [
        'are all configs compatible with all fleasions?',
        'can any mods tell me why my config got deleted?',
        'did my config get deleted?',
        'check the configs channel in there i think theres a few there',
        'ive been looking on yt its js a bunch of tutorials and not much configs',
        'check youtube if there is nothing in #configs',
        'i tried sum cfg but doesnt work',
        'when i use the archkatana cnfg why does it not show the profile picture',
        'how do i make my boneclaw cnfg work',
        'why is the tag CNFG',
        'how to upload the config',
        'I need help with my config',
        'I need some help with this cfg',
        'I want to fix my config',
        'is there a reason my config will not load?',
        'is there a way to fix this config?',
        'does anyone know why my config stopped working?',
        'does anyone know how to make my config work?',
        'anyone have configuration advice?',
        'anyone have configparser installed?',
        'config',
        '',
    ],
)
def test_does_not_redirect_config_discussion(content: str) -> None:
    assert not HelpMessageClassifier.BINDINGS[HelpMessageEnum.CONFIG](content)
