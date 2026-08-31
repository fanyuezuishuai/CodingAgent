# include <bits/stdc++.h>
using namespace std;

int n;
// true: 有棋子, 不许放
vector<bool> column;
vector<bool> diagonal_up; // 左下到右上对角线 
vector<bool> diagonal_down_left;// 左上到右下对角线 且在左下角(row-col>0)
vector<bool> diagonal_down_right; // 左上到右下对角线 且在右上角(col-row>0)

vector<vector<int>> results;
vector<int> path;
int ans = 0;

void dfs(int row){
    if (row > n) {
        ans++;
        if (results.size() < 3)
        {
            results.push_back(path);
        }
        return;
    }
    for (int col = 1; col <= n; col++){
        if (column[col]) continue;
        if (diagonal_up[row+col]) continue;
        if (row - col >= 0 && diagonal_down_left[row - col]) continue;
        if (col-row >= 0 && diagonal_down_right[col-row]) continue;

        // 不能这样, 因为不确定这是不是一个解
        // results[solution_count].push_back(col);
        path.push_back(col);
        column[col] = true;
        diagonal_up[row + col] = true;
        if (row - col >= 0)
        {
            diagonal_down_left[row - col] = true;
        }
        if (col - row >= 0)
        {
            diagonal_down_right[col - row] = true;
        }
        dfs(row + 1);
        // ans++; 放错位置了

        // 回溯
        path.pop_back(); // 别忘了
        column[col] = false;
        diagonal_up[row + col] = false;
        if (row - col >= 0)
        {
            diagonal_down_left[row - col] = false;
        }
        if (col - row >= 0)
        {
            diagonal_down_right[col - row] = false;
        }
    }
}

int main(){
    cin >> n;
    column.resize(n+1, false);
    diagonal_up.resize(2*n+1, false);
    diagonal_down_left.resize(n, false);
    diagonal_down_right.resize(n, false);
    dfs(1);
    for (int i = 0; i < 3; i++){
        for (int j = 0; j < n; j++){
            cout << results[i][j] << ' ';
        }
        cout << endl;
    }
    cout << ans;
    return 0;
}