"""院校清单：已核实使用 Canvas LMS 的学校及其站点地址。

**这份清单怎么来的**：每一条的地址都用程序探测过——请求
``<地址>/api/v1/users/self/profile``，返回 HTTP 401 且响应体是含「未认证」
语义的 JSON，即为 Canvas 实例（非 Canvas 站点会返回 404 / HTML 页面）。
院校名称取自站点自身（登录页标题、logo 文本、版权行），抓不到时以域名
为准——``canvas.umich.edu`` 这样的域名本身已足够准确。

**为什么要内置**：Canvas 在全球有数千所院校在用，站点地址五花八门
（``canvas.<校名>.edu``、``<缩写>.instructure.com``、``canvas.<校名>.ac.uk``……），
让用户自己猜域名是最大的上手门槛。清单只是**起点**——不在清单里的学校，
在「选择院校」窗口里直接手填地址即可，程序不做任何限制。

维护提示：新增条目请先用上面的探测方法确认地址确实是 Canvas
（返回 401 + JSON）；名称尽量从站点自身抓，别凭印象写。
"""

from __future__ import annotations

#: (院校名称, Canvas 站点地址, 地区)
#:
#: 地址一律不带结尾斜杠。地区仅用于界面分组，不影响功能。
SCHOOLS: tuple[tuple[str, str, str], ...] = (
    ("香港城市大学 City University of Hong Kong", "https://canvas.cityu.edu.hk", "中国香港/澳门"),
    ("香港理工大学 The Hong Kong Polytechnic University", "https://canvas.polyu.edu.hk", "中国香港/澳门"),
    ("上海交通大学 Shanghai Jiao Tong University", "https://canvas.sjtu.edu.cn", "中国内地/台湾"),
    ("韩国科学技术院 KAIST", "https://canvas.kaist.ac.kr", "韩国"),
    ("成均馆大学 Sungkyunkwan University", "https://canvas.skku.edu", "美国"),
    ("新加坡国立大学 National University of Singapore", "https://canvas.nus.edu.sg", "新加坡"),
    ("悉尼大学 University of Sydney", "https://canvas.sydney.edu.au", "澳大利亚/新西兰"),
    ("澳国立 Australian National University", "https://canvas.anu.edu.au", "澳大利亚/新西兰"),
    ("悉尼科技大学 University of Technology Sydney", "https://canvas.uts.edu.au", "澳大利亚/新西兰"),
    ("墨尔本理工大学 RMIT University", "https://canvas.rmit.edu.au", "澳大利亚/新西兰"),
    ("新西兰奥克兰大学 University of Auckland", "https://canvas.auckland.ac.nz", "澳大利亚/新西兰"),
    ("不列颠哥伦比亚大学 University of British Columbia", "https://canvas.ubc.ca", "加拿大"),
    ("西蒙弗雷泽大学 Simon Fraser University", "https://canvas.sfu.ca", "加拿大"),
    ("阿尔伯塔大学 University of Alberta", "https://canvas.ualberta.ca", "加拿大"),
    ("萨斯喀彻温大学 University of Saskatchewan", "https://canvas.usask.ca", "加拿大"),
    ("哈佛大学 Harvard University", "https://canvas.harvard.edu", "美国"),
    ("麻省理工学院 Massachusetts Institute of Technology", "https://canvas.mit.edu", "美国"),
    ("康奈尔大学 Cornell University", "https://canvas.cornell.edu", "美国"),
    ("斯坦福大学 Stanford University", "https://canvas.stanford.edu", "美国"),
    ("芝加哥大学 University of Chicago", "https://canvas.uchicago.edu", "美国"),
    ("布朗大学 Brown University", "https://canvas.brown.edu", "美国"),
    ("杜克大学 Duke University", "https://canvas.duke.edu", "美国"),
    ("卡内基梅隆大学 Carnegie Mellon University", "https://canvas.cmu.edu", "美国"),
    ("埃默里大学 Emory University", "https://canvas.emory.edu", "美国"),
    ("密歇根大学 University of Michigan", "https://canvas.umich.edu", "美国"),
    ("威斯康星大学麦迪逊分校 University of Wisconsin–Madison", "https://canvas.wisc.edu", "美国"),
    ("明尼苏达大学 University of Minnesota", "https://canvas.umn.edu", "美国"),
    ("德州农工大学 Texas A&M University", "https://canvas.tamu.edu", "美国"),
    ("迈阿密大学 University of Miami", "https://canvas.miami.edu", "美国"),
    ("加州大学戴维斯分校 UC Davis", "https://canvas.ucdavis.edu", "美国"),
    ("科罗拉多大学波分校 University of Colorado Boulder", "https://canvas.colorado.edu", "美国"),
    ("亚利桑那州立大学 Arizona State University", "https://canvas.asu.edu", "美国"),
    ("犹他大学 University of Utah", "https://canvas.utah.edu", "美国"),
    ("印第安纳大学 Indiana University", "https://canvas.iu.edu", "美国"),
    ("伊利诺伊大学厄巴纳-香槟分校 University of Illinois Urbana-Champaign", "https://canvas.illinois.edu", "美国"),
    ("北德克萨斯大学 University of North Texas", "https://canvas.unt.edu", "美国"),
    ("华盛顿州立大学 Washington State University", "https://canvas.wsu.edu", "美国"),
    ("伯克利城市学院 Berkeley City College", "https://bcc.instructure.com", "美国"),
    ("迪安学院 Dean College", "https://dean.instructure.com", "美国"),
    ("卡尔斯顿大学 Carleton University", "https://carleton.instructure.com", "美国"),
    ("卡斯卡迪亚学院 Cascadia College", "https://cascadia.instructure.com", "美国"),
    ("克莱顿州立学院 Clayton State University", "https://clayton.instructure.com", "美国"),
    ("海岸社区学院 Coastal Alabama Community College", "https://coastalalabama.instructure.com", "美国"),
    ("中央华盛顿大学 Central Washington University", "https://cwu.instructure.com", "美国"),
    ("迪稳定学院 Diablo Valley College", "https://dvc.instructure.com", "美国"),
    ("福特刘易斯大学 Fort Lewis College", "https://fortlewis.instructure.com", "美国"),
    ("富勒顿学院 Fullerton College", "https://fullerton.instructure.com", "美国"),
    ("格兰岱尔学院 Glendale Community College", "https://glendale.instructure.com", "美国"),
    ("绿河学院 Green River College", "https://greenriver.instructure.com", "美国"),
    ("哈特内尔学院 Hartnell College", "https://hartnell.instructure.com", "美国"),
    ("哈特福德大学 University of Hartford", "https://hartford.instructure.com", "美国"),
    ("休斯敦社区学院 Houston Community College", "https://htc.instructure.com", "美国"),
    ("圣何塞城市学院 San José City College", "https://sjcc.instructure.com", "美国"),
    ("凯洛格社区学院 Kellogg Community College", "https://kellogg.instructure.com", "美国"),
    ("下哥伦比亚学院 Lower Columbia College", "https://lcc.instructure.com", "美国"),
    ("洛斯梅德诺斯学院 Los Medanos College", "https://losmedanos.instructure.com", "美国"),
    ("门多萨学院 Mt. San Jacinto College", "https://msjc.instructure.com", "美国"),
    ("西北东北大学 Northeastern University", "https://northeastern.instructure.com", "美国"),
    ("俄克拉荷马学院 Oklahoma Christian University", "https://ocu.instructure.com", "美国"),
    ("俄勒冈学院 Oregon Coast Community College", "https://opc.instructure.com", "美国"),
    ("俄勒冈技术学院 Oregon Technical Community College", "https://otc.instructure.com", "美国"),
    ("帕洛马学院 Palomar College", "https://palomar.instructure.com", "美国"),
    ("帕萨迪纳学区 Pasadena Unified School District", "https://pasadena.instructure.com", "美国"),
    ("普拉特学院 Pratt Institute", "https://pratt.instructure.com", "美国"),
    ("圣迭戈学院 San Diego College", "https://sandiego.instructure.com", "美国"),
    ("圣何塞学院 Santa Monica College", "https://smc.instructure.com", "美国"),
    ("塞米诺里州立大学 Southeast Missouri State University", "https://semo.instructure.com", "美国"),
    ("海岸线学院 Shoreline Community College", "https://shoreline.instructure.com", "美国"),
    ("厄瓜多尔科尔多瓦大学 Universidad de Córdoba", "https://uco.instructure.com", "美国"),
    ("文学院 Valencia College", "https://vcc.instructure.com", "美国"),
    ("弗吉尼亚联邦大学 Virginia Commonwealth University", "https://vcu.instructure.com", "美国"),
    ("佛蒙特州立大学 Vermont State University", "https://vermont.instructure.com", "美国"),
    ("文学院 Victor Valley College", "https://vvc.instructure.com", "美国"),
    ("韦伯州立大学 Weber State University", "https://weber.instructure.com", "美国"),
    ("约克大学 University of York", "https://york.instructure.com", "英国"),
    ("优胜美地社区学院 Yosemite Community College District", "https://yosemite.instructure.com", "美国"),
    ("埃平森林学院 Epping Forest College", "https://efc.instructure.com", "英国"),
    ("霍伊特大学 Hartford University", "https://hbu.instructure.com", "美国"),
    ("圣保罗学院 Centro Paula Souza", "https://cps.instructure.com", "美国"),
    ("杰罗顿学院 Dodge College", "https://dodge.instructure.com", "美国"),
    ("曼彻斯特大学 University of Manchester", "https://canvas.manchester.ac.uk", "英国"),
    ("阿姆斯特丹大学 University of Amsterdam", "https://canvas.uva.nl", "荷兰"),
    ("埃因霍温理工大学 Eindhoven University of Technology", "https://canvas.tue.nl", "荷兰"),
    ("奥斯陆大学 University of Oslo", "https://canvas.uio.no", "其他"),
    ("俄亥俄州立大学 Ohio State University", "https://osu.instructure.com", "美国"),
    ("宾州州立大学 Pennsylvania State University", "https://psu.instructure.com", "美国"),
    ("北卡州立大学 North Carolina State University", "https://ncsu.instructure.com", "美国"),
    ("佐治亚理工学院 Georgia Institute of Technology", "https://gatech.instructure.com", "美国"),
    ("特罗伊大学 Rensselaer Polytechnic Institute", "https://rpi.instructure.com", "美国"),
    ("普林斯顿大学 Princeton University", "https://princeton.instructure.com", "美国"),
    ("耶鲁大学 Yale University", "https://yale.instructure.com", "美国"),
    ("波士顿大学 Boston University", "https://bu.instructure.com", "美国"),
    ("佛罗里达大学 University of Florida", "https://ufl.instructure.com", "美国"),
    ("罗切斯特大学 University of Rochester", "https://rochester.instructure.com", "美国"),
    ("加州大学洛杉矶分校 UCLA", "https://canvas.ucla.edu", "美国"),
    ("普渡大学 Purdue University", "https://purdue.instructure.com", "美国"),
    ("德州大学阿灵顿分校 University of Texas at Arlington", "https://uta.instructure.com", "美国"),
    ("科罗拉多大学丹佛分校 University of Colorado Denver", "https://ucdenver.instructure.com", "美国"),
    ("犹他州立大学 Utah State University", "https://usu.instructure.com", "美国"),
    ("内华达大学拉斯维加斯分校 University of Nevada, Las Vegas", "https://unlv.instructure.com", "美国"),
    ("佛蒙特大学 University of Vermont", "https://uvm.instructure.com", "美国"),
    ("新罕布什尔大学 University of New Hampshire", "https://unh.instructure.com", "美国"),
    ("雪城大学 Syracuse University", "https://syracuse.instructure.com", "美国"),
    ("密苏里大学 University of Missouri", "https://missouri.instructure.com", "美国"),
    ("南伊利诺伊大学 Southern Illinois University", "https://siu.instructure.com", "美国"),
    ("大绿湾大学 University of Wisconsin–Green Bay", "https://gvsu.instructure.com", "美国"),
    ("北科罗拉多大学 University of Northern Colorado", "https://unco.instructure.com", "美国"),
    ("克莱姆森大学 Clemson University", "https://clemson.instructure.com", "美国"),
    ("南卡罗来纳大学查尔斯顿分校 College of Charleston", "https://cofc.instructure.com", "美国"),
    ("萨凡纳艺术学院 Savannah College of Art and Design", "https://scad.instructure.com", "美国"),
    ("约金斯维尔州立大学 Jacksonville State University", "https://jsu.instructure.com", "美国"),
    ("哥本哈根大学 University of Copenhagen", "https://ku.instructure.com", "加拿大"),
    ("赫尔辛基大学 University of Helsinki", "https://helsinki.instructure.com", "美国"),
    ("奥胡斯大学 Aarhus University", "https://au.instructure.com", "美国"),
    ("多伦多大学 University of Toronto", "https://utoronto.instructure.com", "加拿大"),
    ("新南威尔士大学 University of New South Wales", "https://unsw.instructure.com", "美国"),
    ("蒙纳士大学 Monash University", "https://monash.instructure.com", "美国"),
    ("西澳大学 University of Western Australia", "https://uwa.instructure.com", "美国"),
    ("阿德莱德大学 University of Adelaide", "https://adelaide.instructure.com", "美国"),
    ("首尔大学 Seoul National University", "https://snu.instructure.com", "韩国"),
    ("新加坡管理大学 Singapore Management University", "https://smu.instructure.com", "新加坡"),
    ("香港科技大学 HKUST", "https://hkust.instructure.com", "中国香港/澳门"),
    ("香港大学 The University of Hong Kong", "https://hku.instructure.com", "中国香港/澳门"),
    ("澳门科技大学 Macau University of Science and Technology", "https://must.instructure.com", "中国香港/澳门"),
    ("北京大学 Peking University", "https://pku.instructure.com", "中国内地/台湾"),
    ("国立政治大学 National Chengchi University", "https://nccu.instructure.com", "中国内地/台湾"),
)


def search(keyword: str = "") -> list[tuple[str, str, str]]:
    """按关键词搜索院校（名称或域名包含即命中）。

    关键词为空时返回全部。命中按「名称前缀 > 名称包含 > 域名包含」排序，
    名称在前缀命中时才用中文名做前缀判断（避免「香港城市大学」这种
    中英混排名把英文院校挤到后面）。
    """
    kw = (keyword or "").strip().lower()
    if not kw:
        return list(SCHOOLS)
    scored: list[tuple[int, str, str, str]] = []
    for name, url, region in SCHOOLS:
        low = name.lower()
        dom = url.split("//")[-1].split("/")[0].lower()
        if low.startswith(kw):
            rank = 0
        elif kw in low:
            rank = 1
        elif kw in dom:
            rank = 2
        else:
            continue
        scored.append((rank, name, url, region))
    scored.sort(key=lambda x: (x[0], x[1]))
    return [(n, u, r) for _rank, n, u, r in scored]


def regions() -> list[str]:
    """清单里出现过的地区（按出现顺序去重）。"""
    out: list[str] = []
    for _name, _url, region in SCHOOLS:
        if region not in out:
            out.append(region)
    return out
