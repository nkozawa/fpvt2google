//const url = "https://docs.google.com/spreadsheets/d/1ArZOQzExKNQ1GVvxW2IaAUTiaSKguhe6OOBOBisOe-8/edit?gid=12949935#gid=12949935";
//const sheet = SpreadsheetApp.openByUrl(url);
const sheet = SpreadsheetApp.getActiveSpreadsheet();
const rawdataSheet = sheet.getSheetByName('RAWDATA');
const qualifySheet = sheet.getSheetByName('予選順位表');
const finalSheet = sheet.getSheetByName('順位表');

// todo
// - コメントの日本語化
//
// 書き込み先: 予選（Type=qualify）は「順位表」と「予選順位表」の両方、
// それ以外（公式練習 practice / 勝ち上がり・決勝 final）は「順位表」だけ。
// 予選順位表は予選終了時の内容を保持しておくためのものなので、
// 公式練習や勝ち上がりの結果で上書きしない。

//function test() {
//  let rawdataSheet = sheet.getSheetByName('RAWDATA');
//  rawdataSheet.appendRow(["Test"]);

//  let range = calcSheet.getRange("H5");
//  let sheetName = range.getValue();
//  Logger.log(sheetName);
//}

function trans(status) {
  if (status == "") return " ";
  let status2 = status.replace('practice', '公式練習');
  status2 = status2.replace('cut', '順位確定(予選)');
  status2 = status2.replace('out', '順位確定(勝ち上がり戦)');
  status2 = status2.replace('advances','上位へ勝ち上がり');
  status2 = status2.replace('enters','勝ち上がり戦');
  status2 = status2.replace('finalist', '決勝戦進出')
  status2 = status2.replace('final', '決勝戦')
  return status2;
}

function dataAnal(timestamp, type, stagesData) {
  // ステージ名
  var stageName = stagesData[0].Name;
  Logger.log("ステージ名: " + stageName);
  // ヘッダー情報（"Laps", "Time", "Status"）
  var headings = stagesData[0].Standings.Headings;
  Logger.log("ヘッダー: " + headings.join(", "));

  // 書き込み先シートの決定（qualify のときだけ予選順位表にも書く）
  let targetSheets = [];
  if (type == "qualify") {
    targetSheets = [finalSheet, qualifySheet];
  } else {
    targetSheets = [finalSheet];
  }

  let result = [];
  var rows = stagesData[0].Standings.Rows;
  rows.forEach(function(row, index) {
    var pilotName = row.Name;          // 選手名 (例: Shu_FPV)
    //var pilotId = row.PilotId;          // パイロットID
    var laps = row.Values[0];           // Laps (Values配列の0番目)
    var time = row.Values[1];           // Time (Values配列の1番目)
    var status = (typeof row.Values[2] == 'undefined') ? "" : trans(row.Values[2]);
    result.push([index + 1, pilotName, laps, time, "", status]);  
    Logger.log((index + 1) + "位(?): " + pilotName + " (Laps: " + laps + ", Time: " + time + ", Status: " + status + ")");
  });
  
  for (let i = result.length; i <= 100; i++) {
    result.push(["","","","","",""]);
  }

  // 対象となるすべてのシートに同じデータを書き込む
  targetSheets.forEach(function(sheet) {
    sheet.getRange("G3").setValue(stageName);
    sheet.getRange(5, 2, result.length, result[0].length).setValues(result);
  });
}

function doPost(e) {
  // POSTされたデータをJSONとしてパース
  var inputData = JSON.parse(e.postData.getDataAsString());
  
  const lock = LockService.getScriptLock();
  if (lock.tryLock(10000)) {
    var stagesData = inputData.stages;
//    rawdataSheet.appendRow(["TEST","TEST"]);
    rawdataSheet.appendRow([inputData.timestamp, inputData.Type, stagesData[0].Name]);
    dataAnal(inputData.timestamp, inputData.Type, stagesData);    
    lock.releaseLock();
  } else {
    throw new Error("Lock timeout");
  }
}

