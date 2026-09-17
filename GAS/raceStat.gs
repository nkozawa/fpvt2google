const sheet = SpreadsheetApp.getActiveSpreadsheet();

//function test() {
//  let rawdataSheet = sheet.getSheetByName('RAWDATA');
//  rawdataSheet.appendRow(["Test"]);

//  let range = calcSheet.getRange("H5");
//  let sheetName = range.getValue();
//  Logger.log(sheetName);
//}

// Tiny View Plus OSC process

function raceStatus(timestamp, camera, laps, laptime, totaltime, pilotname) {
  var statusSheet = sheet.getSheetByName('RaceStatus');
  if (camera == 0) {
    if (laps == "started") {
      statusSheet.getRange("B1:E22").clear();
      statusSheet.getRange("B1:E22").setBackground("Gray")
      statusSheet.getRange("B23:E26").clearContent();
    }
    return;
  }
  
  var col = "H";
  var color = "White";
  if (Math.abs(camera) == 1) {
    col = "B";
    color = "Red";
    fontColor = "White";
  } else if (Math.abs(camera) == 2) {
    col = "C";
    color = "Green";
    fontColor = "White";
  } else if (Math.abs(camera) == 3) {
    col = "D";
    color = "Blue";
    fontColor = "White";
  } else if (Math.abs(camera) == 4) {
    col = "E";
    color = "Yellow";
    fontColor = "Black";
  }

  if (0 < camera) {
    statusSheet.getRange(col+"26").setValue(pilotname);
    if (laps == 1) {
      statusSheet.getRange(col+"25").setValue(totaltime);
      statusSheet.getRange(col+"24").setValue(laptime);
      statusSheet.getRange(col+"23").setValue(laptime);
    } else if (laps > 1) {
      let total = Number(statusSheet.getRange(col+"25").getValue());
      let bestlap = Number(statusSheet.getRange(col+"24").getValue());
      let lastlap = Number(statusSheet.getRange(col+"23").getValue());
      statusSheet.getRange(col+35).setValue(total);
      statusSheet.getRange(col+34).setValue(bestlap);
      statusSheet.getRange(col+33).setValue(lastlap);
      let currentlap = Number(laptime);
      if (currentlap < bestlap) {
        statusSheet.getRange(col+"24").setValue(currentlap);
      }
      statusSheet.getRange(col+"23").setValue(Number(currentlap));
      statusSheet.getRange(col+"25").setValue(Number(totaltime));
//      statusSheet.getRange(col+"25").setValue(Number(total+currentlap));
    }
    let bar = statusSheet.getRange(col+(23-laps)+":"+col+"22")
    bar.clear();
    bar.setBackground(color);
    let rng = statusSheet.getRange(col+(23-laps));
    rng.setFontColor(fontColor);
    rng.setHorizontalAlignment("center");
    rng.setValue(laps);
  } else if (camera < 0) {
    if (laps == 1) {
      statusSheet.getRange(col+"25").setValue("");
      statusSheet.getRange(col+"24").setValue("");
      statusSheet.getRange(col+"23").setValue("");
    } else if ( laps > 1) {
      let total = Number(statusSheet.getRange(col+"35").getValue());
      let bestlap = Number(statusSheet.getRange(col+"34").getValue());
      let lastlap = Number(statusSheet.getRange(col+"33").getValue());
      statusSheet.getRange(col+25).setValue(total);
      statusSheet.getRange(col+24).setValue(bestlap);
      statusSheet.getRange(col+23).setValue(lastlap);
    }
    let rng = statusSheet.getRange(col+(23-laps))
    rng.clear();
    rng.setBackground("Gray");
  }
}

function dataAnal(timestamp, type, pos, channel, pilot, lap, total, laptime) {
  if (type == "RaceStart") {
    raceStatus(timestamp, 0, "started", "", "", "");
  } else {
    raceStatus(timestamp, pos, lap, laptime, total, pilot);
  }
}

function doPost(e) {
  // POSTされたデータをJSONとしてパース
  var inputData = JSON.parse(e.postData.getDataAsString());
  var rawdataSheet = sheet.getSheetByName('RAWDATA');
  
  const lock = LockService.getScriptLock();
  if (lock.tryLock(10000)) {
//    rawdataSheet.appendRow(["TEST","TEST"]);
    rawdataSheet.appendRow([inputData.timestamp, inputData.type, inputData.pos, inputData.channel, inputData.pilot, inputData.lap, inputData.holeshot, inputData.total, inputData.laptime, inputData.round, inputData.race, inputData.position, inputData.finished]);
    dataAnal(inputData.timestamp, inputData.type, inputData.pos, inputData.channel, inputData.pilot, inputData.lap, inputData.total, inputData.laptime);    
    lock.releaseLock();
  } else {
    throw new Error("Lock timeout");
  }
}
// End of Tiny View Plus OSC process

