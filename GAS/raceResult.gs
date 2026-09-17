const sheet = SpreadsheetApp.getActiveSpreadsheet();
const rawdataSheet = sheet.getSheetByName('RAWDATA');
const qualifySheet = sheet.getSheetByName('予選順位表');
const finalSheet = sheet.getSheetByName('最終順位表');

//function test() {
//  let rawdataSheet = sheet.getSheetByName('RAWDATA');
//  rawdataSheet.appendRow(["Test"]);

//  let range = calcSheet.getRange("H5");
//  let sheetName = range.getValue();
//  Logger.log(sheetName);
//}

function dataAnal(timestamp, type, stagesData) {
  // ステージ名
  var stageName = stagesData[0].Name;
  Logger.log("ステージ名: " + stageName);
  // ヘッダー情報（"Laps", "Time", "Status"）
  var headings = stagesData[0].Standings.Headings;
  Logger.log("ヘッダー: " + headings.join(", "));
  const resultSheet = (type == "final") ? finalSheet : qualifySheet;

  resultSheet.getRange("G3").setValue(stageName);

  let result = [];
  // 各選手のデータをループで処理
  var rows = stagesData[0].Standings.Rows;
  rows.forEach(function(row, index) {
    var pilotName = row.Name;          // 選手名 (例: Shu_FPV)
    //var pilotId = row.PilotId;          // パイロットID
    var laps = row.Values[0];           // Laps (Values配列の0番目)
    var time = row.Values[1];           // Time (Values配列の1番目)
    var status = row.Values[2];         // Status (Values配列の2番目)
    result.push([index + 1, pilotName, laps, time, "", status]);  
    Logger.log((index + 1) + "位(?): " + pilotName + " (Laps: " + laps + ", Time: " + time + ", Status: " + status + ")");
  });
  for (let i = result.length; i <= 100; i++) {
    result.push(["","","","","",""]);
  }
  resultSheet.getRange(5, 2, result.length, result[0].length).setValues(result);

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
// End of Tiny View Plus OSC process

